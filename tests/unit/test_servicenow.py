"""ServiceNow adapter against a mocked Table API: record mapping, polling, approval mirroring, write-back."""

from __future__ import annotations

import json
from urllib.parse import parse_qs

import httpx
import pytest

from gca.config import ServiceNowConfig
from gca.models import Job, JobStatus, RunResult, WorkItem
from gca.queue import JobQueue
from gca.sources.servicenow import ServiceNowSource, sync_once


class FakeTableAPI:
    """Just enough of /api/now/table/<t> for the adapter: GET with sysparm_query, PATCH by sys_id."""

    def __init__(self, records):
        self.records = {r["sys_id"]: dict(r) for r in records}
        self.patches: list[tuple[str, dict]] = []
        self.requests: list[httpx.Request] = []

    def handler(self, req: httpx.Request) -> httpx.Response:
        self.requests.append(req)
        assert req.headers.get("authorization", "").startswith("Basic ")
        if req.method == "GET":
            q = parse_qs(req.url.query.decode())["sysparm_query"][0]
            rows = list(self.records.values())
            if "state=approved^gca_job_idISEMPTY" in q:
                rows = [r for r in rows if r["state"] == "approved" and not r.get("gca_job_id")]
            elif "gca_job_idISNOTEMPTY" in q:
                rows = [r for r in rows if r.get("gca_job_id") and r["state"] in ("approved", "running")]
            return httpx.Response(200, json={"result": rows})
        if req.method == "PATCH":
            sys_id = req.url.path.rsplit("/", 1)[-1]
            body = json.loads(req.content)
            self.patches.append((sys_id, body))
            self.records[sys_id].update(body)
            return httpx.Response(200, json={"result": self.records[sys_id]})
        return httpx.Response(405)


REC = {
    "sys_id": "abc123", "number": "WI0001001", "state": "approved", "short_description": "Fix mean()",
    "description": "off by one", "acceptance_criteria": "mean divides by n\nall tests pass\n",
    "repo_path": "/repos/calc", "base_ref": "main", "test_command": "python -m pytest -q",
    "allowed_paths": "src, tests", "opened_by": {"value": "u1", "display_value": "Ashwin Prabhu"},
    "approver": {"value": "u2", "display_value": "Tech Lead"}, "gca_job_id": "",
}


@pytest.fixture
def env(monkeypatch):
    monkeypatch.setenv("SN_USER", "svc_gca")
    monkeypatch.setenv("SN_PASSWORD", "pw")


def make_source(api: FakeTableAPI) -> ServiceNowSource:
    return ServiceNowSource(ServiceNowConfig(instance_url="https://dev1.service-now.com/"),
                            transport=httpx.MockTransport(api.handler))


def test_record_maps_to_work_item(env):
    src = make_source(FakeTableAPI([REC]))
    wi = src.to_work_item(REC)
    assert wi.title == "Fix mean()" and wi.acceptance_criteria == ["mean divides by n", "all tests pass"]
    assert wi.test_command == ["python", "-m", "pytest", "-q"] and wi.allowed_paths == ["src", "tests"]
    assert wi.requester == "Ashwin Prabhu" and wi.source == "servicenow" and wi.source_ref == "abc123"
    assert wi.idempotency_key == "servicenow:abc123" and wi.base_ref == "main"
    with pytest.raises(ValueError, match="repo_path"):
        src.to_work_item({**REC, "repo_path": ""})


def test_missing_instance_url_is_an_error():
    with pytest.raises(ValueError, match="instance_url"):
        ServiceNowSource(ServiceNowConfig())


def test_sync_queues_approved_records_and_mirrors_approval(env, tmp_path, config):
    api = FakeTableAPI([REC, {**REC, "sys_id": "req1", "number": "WI0001002", "state": "requested"}])
    src = make_source(api)
    q = JobQueue(config.db_path)
    rep = sync_once(config, q, src)
    assert len(rep["queued"]) == 1 and not rep["errors"]
    jobs = q.list()
    assert len(jobs) == 1 and jobs[0].status == JobStatus.APPROVED
    assert jobs[0].approval.approver == "Tech Lead" and "WI0001001" in jobs[0].approval.note
    assert jobs[0].work_item.idempotency_key == "servicenow:abc123"
    # write-back recorded the job id and state on the record
    sys_id, body = api.patches[0]
    assert sys_id == "abc123" and body["gca_job_id"] == jobs[0].id and body["state"] == "approved"
    # audit log shows the ServiceNow approver, not gca
    from gca.audit import AuditLog
    actors = [e.actor for e in AuditLog(config.audit_dir, jobs[0].id).events()]
    assert actors == ["Ashwin Prabhu", "Tech Lead"]


def test_sync_is_idempotent_across_cycles(env, config):
    api = FakeTableAPI([REC])
    src = make_source(api)
    q = JobQueue(config.db_path)
    sync_once(config, q, src)
    # simulate the write-back not having landed (record still has empty gca_job_id)
    api.records["abc123"]["gca_job_id"] = ""
    sync_once(config, q, src)
    assert len(q.list()) == 1  # idempotency key prevented a duplicate job


def test_sync_writes_back_terminal_status_with_audit_hash(env, config):
    api = FakeTableAPI([REC])
    src = make_source(api)
    q = JobQueue(config.db_path)
    sync_once(config, q, src)
    job = q.list()[0]
    # drive the job to a terminal state locally
    q.claim_next("w1")
    q.transition(job.id, JobStatus.VERIFYING)
    q.finish(job.id, JobStatus.SUCCEEDED, result=RunResult(job_id=job.id, branch=f"gca/{job.id}", report_markdown="# ok"))
    api.records["abc123"]["state"] = "running"  # what ServiceNow last saw
    rep = sync_once(config, q, src)
    assert rep["updated"] == ["WI0001001:succeeded"]
    sys_id, body = api.patches[-1]
    assert body["state"] == "succeeded" and body["gca_branch"] == f"gca/{job.id}" and body["gca_report"] == "# ok"
    assert len(body["gca_audit_hash"]) == 64


def test_one_bad_record_does_not_stop_the_cycle(env, config):
    bad = {**REC, "sys_id": "bad1", "number": "WI0001003", "repo_path": ""}
    api = FakeTableAPI([bad, {**REC, "sys_id": "ok1", "number": "WI0001004"}])
    src = make_source(api)
    q = JobQueue(config.db_path)
    rep = sync_once(config, q, src)
    assert len(rep["errors"]) == 1 and "WI0001003" in rep["errors"][0]
    assert len(rep["queued"]) == 1 and len(q.list()) == 1


def test_http_errors_surface(env, config):
    def boom(req):
        return httpx.Response(401, json={"error": {"message": "User Not Authenticated"}})
    src = ServiceNowSource(ServiceNowConfig(instance_url="https://dev1.service-now.com"),
                           transport=httpx.MockTransport(boom))
    with pytest.raises(httpx.HTTPStatusError):
        src.fetch_approved()
    job = Job(work_item=WorkItem(title="t", description="d", acceptance_criteria=[], repo_path="/r"))
    with pytest.raises(httpx.HTTPStatusError):
        src.write_back("x", job)
