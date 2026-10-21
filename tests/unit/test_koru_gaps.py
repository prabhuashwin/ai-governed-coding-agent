"""The three Koru integration gaps: per-agent stop, approval deadline, opaque identities ("forget user")."""

from __future__ import annotations

import json
import sqlite3
import sys
import threading
import time
import uuid
from contextlib import closing

import httpx
import pytest
from conftest import steps_fix_calc
from pydantic import ValidationError

from gca import webhooks
from gca.approvals import approval_deadline, expire_overdue
from gca.audit import AuditLog
from gca.cli import main as cli_main
from gca.config import MAX_APPROVAL_TIMEOUT_SECONDS, ApprovalConfig, PrivacyConfig, WebhookConfig
from gca.inbound import CommandHandler, set_kill_switch
from gca.models import Job, JobStatus, StopReason, WorkItem
from gca.providers.testing import ScriptedProvider
from gca.queue import ApprovalExpired, JobQueue
from gca.webhooks import signed_headers
from gca.worker import Worker

OUT, IN = "out-secret-koru", "in-secret-koru"
PAST = "2000-01-01T00:00:00+00:00"


@pytest.fixture
def events(monkeypatch):
    """Outbound events as the platform receives them."""
    monkeypatch.setenv("GCA_WEBHOOK_SECRET", OUT)
    monkeypatch.setenv("GCA_INBOUND_SECRET", IN)
    got: list[dict] = []

    def handler(req: httpx.Request) -> httpx.Response:
        got.append(json.loads(req.content))
        return httpx.Response(204)

    webhooks.set_default_transport(httpx.MockTransport(handler))
    yield got
    webhooks.set_default_transport(None)
    webhooks.uninstall()


@pytest.fixture
def hook_cfg(config, events):
    config.webhooks = WebhookConfig(enabled=True, outbound_urls=["https://koru.example/hooks/gca"])
    config.approval = ApprovalConfig(mode="webhook")
    webhooks.install(config)
    return config


def of(events: list[dict], etype: str) -> list[dict]:
    return [e for e in events if e["type"] == etype]


def call(h: CommandHandler, method: str, path: str, body: dict | None = None, delivery_id: str | None = None):
    raw = json.dumps(body).encode() if body is not None else b""
    did = delivery_id or f"d-{uuid.uuid4().hex}"
    return h.handle(method, path, signed_headers(IN, method, path, raw, delivery_id=did), raw)


def wi_body(work_item: WorkItem, **kw) -> dict:
    b = {"title": work_item.title, "description": work_item.description,
         "acceptance_criteria": work_item.acceptance_criteria, "repo_path": work_item.repo_path,
         "test_command": work_item.test_command, "requester": "u-dev"}
    b.update(kw)
    return b


def kill(h, enabled, *, scope="agent", agent_id="a1", reason="incident", actor="u-sec", delivery_id=None):
    body = {"enabled": enabled, "actor": actor, "reason": reason, "scope": scope}
    if agent_id is not None:
        body["agent_id"] = agent_id
    return call(h, "POST", "/v1/controls/kill-switch", body, delivery_id)


def backdate_deadline(cfg, job_id: str) -> None:
    """Move a job's approval deadline into the past (both the indexed column and the job body)."""
    with closing(sqlite3.connect(cfg.db_path)) as c, c:
        body = json.loads(c.execute("SELECT body FROM jobs WHERE id=?", (job_id,)).fetchone()[0])
        body["approval_expires_at"] = PAST
        c.execute("UPDATE jobs SET body=?, approval_expires_at=? WHERE id=?", (json.dumps(body), PAST, job_id))


# ======================================================================== 1. per-agent stop
def test_agent_id_is_validated(hook_cfg, work_item):
    assert WorkItem(**{**work_item.model_dump(), "agent_id": "koru:agent/7_a-b.c"}).agent_id == "koru:agent/7_a-b.c"
    for bad in ("has space", "a@b.example", "", "-leading", "x" * 129):
        with pytest.raises(ValidationError):
            WorkItem(**{**work_item.model_dump(), "agent_id": bad})
    h = CommandHandler(hook_cfg, JobQueue(hook_cfg.db_path))
    status, resp = call(h, "POST", "/v1/work-items", wi_body(work_item, agent_id="bad id"))
    assert status == 422


def test_agent_stop_holds_that_agents_queued_jobs_and_spares_others(config, work_item):
    q = JobQueue(config.db_path)
    jobs = {}
    for name, agent in (("a1", "a1"), ("a2", "a2"), ("none", None)):
        j = q.submit(work_item.model_copy(update={"agent_id": agent, "idempotency_key": name}))
        q.approve(j.id, "lead")
        jobs[name] = j.id
    q.set_kill_switch(True, reason="runaway", actor="u-sec", agent_id="a1")
    assert q.stopped_agents() == [{"agent_id": "a1", "reason": "runaway", "set_by": "u-sec",
                                   "set_at": q.stopped_agents()[0]["set_at"]}]
    assert q.kill_switch() is None and q.stop_cause("a1") == "agent_stopped" and q.stop_cause("a2") is None
    claimed = [q.claim_next("w").id, q.claim_next("w").id]
    assert claimed == [jobs["a2"], jobs["none"]]  # a1's job, the oldest, is skipped
    assert q.claim_next("w") is None and q.get(jobs["a1"]).status == JobStatus.APPROVED  # held, not cancelled
    q.set_kill_switch(False, reason="fixed the prompt", actor="u-sec", agent_id="a1")
    assert q.stopped_agents() == [] and q.claim_next("w").id == jobs["a1"]  # released


def test_agent_stop_interrupts_that_agents_running_command(config, work_item):
    q = JobQueue(config.db_path)
    job = q.submit(work_item.model_copy(update={"agent_id": "a1"}))
    q.approve(job.id, "lead")
    steps = [{"tool_calls": [{"name": "run_command", "arguments": {"argv": [sys.executable, "-c", "import time; time.sleep(20)"]}}]},
             {"tool_calls": [{"name": "finish", "arguments": {"summary": "done", "success": True}}]}]

    def stop_soon():
        time.sleep(1.0)
        JobQueue(config.db_path).set_kill_switch(True, reason="drill", actor="u-sec", agent_id="a1")

    threading.Thread(target=stop_soon).start()
    t0 = time.monotonic()
    job = Worker(config, q, provider_factory=lambda: ScriptedProvider(steps)).run_once()
    assert time.monotonic() - t0 < 8, "the agent's running command was not interrupted"
    assert job.status == JobStatus.CANCELLED and job.error == "agent_stopped"
    assert job.result.agent.stop_reason == StopReason.KILL_SWITCH
    assert q.kill_switch() is None  # only that agent was stopped


def test_agent_stop_engaged_before_success_commits_cancels(config, work_item):
    q = JobQueue(config.db_path)
    job = q.submit(work_item.model_copy(update={"agent_id": "a1"}))
    q.approve(job.id, "lead")
    original = JobQueue.complete

    def stop_then_complete(self, job_id, status, *a, **kw):
        if status == JobStatus.SUCCEEDED:
            JobQueue(config.db_path).set_kill_switch(True, reason="drill", actor="u-sec", agent_id="a1")
        return original(self, job_id, status, *a, **kw)

    JobQueue.complete = stop_then_complete  # type: ignore[method-assign]
    try:
        job = Worker(config, q, provider_factory=lambda: ScriptedProvider(steps_fix_calc())).run_once()
    finally:
        JobQueue.complete = original  # type: ignore[method-assign]
    assert job.status == JobStatus.CANCELLED and job.error == "agent_stopped"
    assert "status **cancelled**" in job.result.report_markdown


def test_another_agents_stop_does_not_touch_this_job_and_events_name_the_agent(hook_cfg, work_item, events):
    q = JobQueue(hook_cfg.db_path)
    h = CommandHandler(hook_cfg, q)
    assert kill(h, True, agent_id="a2")[0] == 200
    status, resp = call(h, "POST", "/v1/work-items", wi_body(work_item, agent_id="a1"))
    assert status == 201 and "agent_stopped" not in resp
    jid = resp["job_id"]
    assert of(events, "job.submitted")[0]["data"]["work_item"]["agent_id"] == "a1"
    assert of(events, "approval.requested")[0]["data"]["agent_id"] == "a1"
    assert call(h, "POST", f"/v1/jobs/{jid}/decision", {"decision": "approve", "approver": "u-lead"})[0] == 200
    job = Worker(hook_cfg, q, provider_factory=lambda: ScriptedProvider(steps_fix_calc())).run_once()
    assert job.status == JobStatus.SUCCEEDED
    assert of(events, "job.finished")[0]["data"]["agent_id"] == "a1"
    # a submission for a stopped agent is accepted but held, and says so
    status, resp = call(h, "POST", "/v1/work-items", wi_body(work_item, agent_id="a2"))
    assert status == 201 and resp["agent_stopped"] is True


def test_agent_switch_over_webhook_validates_audits_and_reports(hook_cfg, events):
    q = JobQueue(hook_cfg.db_path)
    h = CommandHandler(hook_cfg, q)
    assert kill(h, True, agent_id=None)[0] == 422  # scope agent needs an agent id
    assert kill(h, True, agent_id="not valid!")[0] == 422
    status, resp = kill(h, True)
    assert status == 200 and resp == {"enabled": True, "actor": "u-sec", "reason": "incident", "scope": "agent",
                                      "agent_id": "a1"}
    assert kill(h, False, reason="")[0] == 422  # releasing needs a reason
    ev = of(events, "control.kill_switch")[0]
    assert ev["job_id"] is None and ev["data"] == {"enabled": True, "reason": "incident", "actor": "u-sec",
                                                   "scope": "agent", "agent_id": "a1"}
    rec = [e for e in AuditLog(hook_cfg.audit_dir, "_controls").events() if e.event == "kill_switch"][-1]
    assert rec.data["scope"] == "agent" and rec.data["agent_id"] == "a1"
    assert call(h, "GET", "/healthz")[1]["kill_switch"] is False  # the instance switch is untouched
    assert kill(h, False, reason="resolved")[0] == 200 and q.kill_switch("a1") is None


def test_agent_switch_is_independent_of_the_instance_switch(config):
    q = JobQueue(config.db_path)
    set_kill_switch(config, q, True, reason="agent drill", actor="u-sec", via="cli", agent_id="a1")
    set_kill_switch(config, q, True, reason="incident", actor="u-sec", via="cli")
    set_kill_switch(config, q, False, reason="agent fixed", actor="u-sec", via="cli", agent_id="a1")
    assert q.kill_switch() is not None and q.stop_cause("a1") == "kill_switch"  # the instance stop still applies
    set_kill_switch(config, q, False, reason="over", actor="u-sec", via="cli")
    assert q.stop_cause("a1") is None and q.stop_cause(None) is None


def test_stale_agent_release_cannot_undo_a_newer_agent_stop(hook_cfg):
    """The instance switch's supersession guarantee holds per agent: an old "off" retried after a newer "on" for
    the same agent is refused, and its replay is refused the same way."""
    q = JobQueue(hook_cfg.db_path)
    h = CommandHandler(hook_cfg, q)
    set_kill_switch(hook_cfg, q, False, reason="resolved", actor="u-sec", via="webhook", delivery_id="off-1", agent_id="a1")
    h._conn.execute("INSERT INTO inbound_deliveries (id, received_at, status, response, first_received_at) "
                    "VALUES (?,?,0,'',?)", ("off-1", "2026-01-01T00:00:00+00:00", "2026-01-01T00:00:00+00:00"))
    assert kill(h, True, delivery_id="on-2")[0] == 200
    status, resp = kill(h, False, reason="resolved", delivery_id="off-1")
    assert status == 409 and "superseded" in resp["error"]
    assert q.kill_switch("a1") is not None, "a stale delivery released a stopped agent"
    status, resp = kill(h, False, reason="resolved", delivery_id="off-1")
    assert status == 409 and resp["replayed"] is True


def test_cli_agent_switch(tmp_path, capsys):
    toml = tmp_path / "gca.toml"
    toml.write_text(f'data_dir = "{(tmp_path / "d").as_posix()}"\n')
    assert cli_main(["-c", str(toml), "kill", "on", "--agent", "a1", "--reason", "drill", "--by", "u-sec"]) == 0
    capsys.readouterr()
    assert cli_main(["-c", str(toml), "kill", "status"]) == 0
    st = json.loads(capsys.readouterr().out)
    assert st["enabled"] is False and [a["agent_id"] for a in st["stopped_agents"]] == ["a1"]
    assert cli_main(["-c", str(toml), "kill", "off", "--agent", "a1"]) == 2  # reason required
    assert cli_main(["-c", str(toml), "kill", "on", "--agent", "bad id"]) == 2
    assert cli_main(["-c", str(toml), "kill", "off", "--agent", "a1", "--reason", "drill over"]) == 0
    capsys.readouterr()
    cli_main(["-c", str(toml), "kill", "status"])
    assert json.loads(capsys.readouterr().out)["stopped_agents"] == []


def test_databases_from_before_agent_ids_still_open_and_claim(tmp_path, work_item):
    db = tmp_path / "old.sqlite3"
    old = Job(work_item=work_item, status=JobStatus.APPROVED).model_dump()
    old["work_item"].pop("agent_id")
    old.pop("approval_expires_at")  # a 0.2.0 job body
    with closing(sqlite3.connect(db)) as c, c:
        c.execute("CREATE TABLE jobs (id TEXT PRIMARY KEY, status TEXT NOT NULL, idempotency_key TEXT UNIQUE, "
                  "created_at TEXT NOT NULL, updated_at TEXT NOT NULL, heartbeat_at TEXT, worker_id TEXT, body TEXT NOT NULL)")
        c.execute("INSERT INTO jobs (id, status, created_at, updated_at, body) VALUES (?,?,?,?,?)",
                  (old["id"], "approved", old["created_at"], old["updated_at"], json.dumps(old)))
    q = JobQueue(db)
    cols = {r[1] for r in q._conn.execute("PRAGMA table_info(jobs)")}
    assert {"agent_id", "approval_expires_at"} <= cols
    q.set_kill_switch(True, reason="x", actor="u", agent_id="a1")  # an agent stop does not hold agent-less jobs
    assert q.claim_next("w").id == old["id"]
    assert q.expire_overdue_approvals() == []
    q.close()


# ======================================================================== 2. approval deadline
def test_deadline_is_the_shorter_of_request_and_operator_limit(config):
    def secs(deadline):
        from datetime import datetime, timezone
        return (datetime.fromisoformat(deadline) - datetime.now(timezone.utc)).total_seconds()

    assert approval_deadline(config, None) is None  # no limit anywhere: wait indefinitely, as before
    assert 3590 < secs(approval_deadline(config, 3600)) <= 3600
    config.approval = ApprovalConfig(timeout_seconds=60)
    assert 50 < secs(approval_deadline(config, None)) <= 60
    assert 50 < secs(approval_deadline(config, 3600)) <= 60  # a request cannot extend the operator's limit
    assert 0 < secs(approval_deadline(config, 10)) <= 10  # but may shorten it
    for bad in (0, -5, True, 1.5, "10", MAX_APPROVAL_TIMEOUT_SECONDS + 1):
        with pytest.raises(ValueError):
            approval_deadline(config, bad)
    with pytest.raises(ValidationError):
        ApprovalConfig(timeout_seconds=0)


def test_late_decision_is_refused_and_the_platform_is_told(hook_cfg, work_item, events):
    q = JobQueue(hook_cfg.db_path)
    h = CommandHandler(hook_cfg, q)
    status, resp = call(h, "POST", "/v1/work-items", wi_body(work_item, approval_timeout_seconds=600, agent_id="a1"))
    assert status == 201 and resp["approval_expires_at"]
    jid = resp["job_id"]
    req = of(events, "approval.requested")[0]["data"]
    assert req["approval_expires_at"] == resp["approval_expires_at"]
    assert of(events, "job.submitted")[0]["data"]["approval_expires_at"] == resp["approval_expires_at"]
    backdate_deadline(hook_cfg, jid)
    decision = {"decision": "approve", "approver": "u-lead"}
    status, resp = call(h, "POST", f"/v1/jobs/{jid}/decision", decision, delivery_id="late-1")
    assert status == 409 and resp["error"].startswith("approval expired")
    job = q.get(jid)
    assert job.status == JobStatus.CANCELLED and job.error == "approval_expired" and job.approval is None
    exp = of(events, "approval.expired")
    assert len(exp) == 1 and exp[0]["job_id"] == jid
    assert exp[0]["data"] == {"expired_at": PAST, "title": work_item.title, "requester": "u-dev", "agent_id": "a1",
                              "source": "webhook", "source_ref": None}
    assert [e.event for e in AuditLog(hook_cfg.audit_dir, jid).events()][-1] == "approval_expired"
    status2, resp2 = call(h, "POST", f"/v1/jobs/{jid}/decision", decision, delivery_id="late-1")
    assert status2 == 409 and resp2["replayed"] is True  # the retry gets the recorded refusal
    assert call(h, "POST", f"/v1/jobs/{jid}/decision", {"decision": "reject", "approver": "u-lead"})[0] == 409
    assert len(of(events, "approval.expired")) == 1  # expired once, reported once


def test_decision_in_time_is_accepted(hook_cfg, work_item):
    q = JobQueue(hook_cfg.db_path)
    h = CommandHandler(hook_cfg, q)
    jid = call(h, "POST", "/v1/work-items", wi_body(work_item, approval_timeout_seconds=600))[1]["job_id"]
    assert call(h, "POST", f"/v1/jobs/{jid}/decision", {"decision": "approve", "approver": "u-lead"})[0] == 200
    assert q.get(jid).status == JobStatus.APPROVED
    assert call(h, "POST", "/v1/work-items", wi_body(work_item, approval_timeout_seconds=0))[0] == 422


def test_the_queue_refuses_late_decisions_even_before_the_sweep(config, work_item):
    q = JobQueue(config.db_path)
    job = q.submit(work_item, approval_expires_at=PAST)
    with pytest.raises(ApprovalExpired):
        q.approve(job.id, "lead")
    with pytest.raises(ApprovalExpired):
        q.reject(job.id, "lead")
    assert q.get(job.id).status == JobStatus.SUBMITTED  # refused, not yet swept
    assert [j.id for j in expire_overdue(config, q)] == [job.id]
    assert expire_overdue(config, q) == []  # once only
    assert q.request_cancel(job.id).status == JobStatus.CANCELLED and q.get(job.id).error == "approval_expired"


def test_a_decision_that_commits_first_wins_over_the_sweep(config, work_item):
    q = JobQueue(config.db_path)
    job = q.submit(work_item, approval_expires_at="2999-01-01T00:00:00+00:00")
    q.approve(job.id, "lead")
    assert expire_overdue(config, q) == [] and q.get(job.id).status == JobStatus.APPROVED
    # and an expired job that was already approved is never swept
    backdate_deadline(config, job.id)
    assert expire_overdue(config, q) == [] and q.get(job.id).status == JobStatus.APPROVED


def test_the_worker_closes_windows_with_nothing_to_claim(config, work_item):
    q = JobQueue(config.db_path)
    job = q.submit(work_item, approval_expires_at=PAST)
    assert Worker(config, q).run_once() is None
    assert q.get(job.id).status == JobStatus.CANCELLED
    assert [e.event for e in AuditLog(config.audit_dir, job.id).events()] == ["approval_expired"]


def test_a_resubmission_keeps_the_first_deadline(hook_cfg, work_item):
    h = CommandHandler(hook_cfg, JobQueue(hook_cfg.db_path))
    first = call(h, "POST", "/v1/work-items", wi_body(work_item, idempotency_key="k1", approval_timeout_seconds=600))[1]
    again = call(h, "POST", "/v1/work-items", wi_body(work_item, idempotency_key="k1", approval_timeout_seconds=5))[1]
    assert again["job_id"] == first["job_id"] and again["approval_expires_at"] == first["approval_expires_at"]


def test_cli_approval_timeout(tmp_path, calc_repo, capsys):
    toml = tmp_path / "gca.toml"
    toml.write_text(f'data_dir = "{(tmp_path / "d").as_posix()}"\n')
    base = ["-c", str(toml), "submit", "--title", "t", "--description", "d", "--repo", str(calc_repo)]
    assert cli_main(base + ["--approval-timeout", "0"]) == 2
    assert cli_main(base + ["--approval-timeout", "1"]) == 0
    jid = capsys.readouterr().out.strip().split()[-1]
    time.sleep(1.5)  # real time, end to end
    assert cli_main(["-c", str(toml), "approve", jid, "--approver", "lead"]) == 2
    assert "approval expired" in capsys.readouterr().err
    q = JobQueue(tmp_path / "d" / "jobs.sqlite3")
    assert q.get(jid).status == JobStatus.CANCELLED and q.get(jid).error == "approval_expired"
    q.close()
    assert cli_main(["-c", str(toml), "approve", jid, "--approver", "lead"]) == 2  # still refused, no traceback


# ======================================================================== 3. opaque identities
@pytest.fixture
def opaque_cfg(hook_cfg):
    hook_cfg.privacy = PrivacyConfig(opaque_identities=True)
    return hook_cfg


def test_opaque_mode_refuses_names_and_emails_at_every_entry_point(opaque_cfg, work_item):
    q = JobQueue(opaque_cfg.db_path)
    h = CommandHandler(opaque_cfg, q)
    for who in ("alice@corp.example", "Alice Smith", ""):
        status, resp = call(h, "POST", "/v1/work-items", wi_body(work_item, requester=who))
        assert status == 422 and "alice" not in json.dumps(resp).lower()  # never echoed back
    no_requester = {k: v for k, v in wi_body(work_item).items() if k != "requester"}
    assert call(h, "POST", "/v1/work-items", no_requester)[0] == 422
    status, resp = call(h, "POST", "/v1/work-items", wi_body(work_item, requester=["alice@corp.example"]))
    assert status == 422 and resp["detail"][0]["loc"] == ["requester"] and "alice" not in json.dumps(resp)
    assert q.list() == []
    status, resp = call(h, "POST", "/v1/work-items", wi_body(work_item, requester="u_7f3a"))
    assert status == 201
    jid = resp["job_id"]
    assert call(h, "POST", f"/v1/jobs/{jid}/decision", {"decision": "approve", "approver": "bob@corp.example"})[0] == 422
    assert call(h, "POST", f"/v1/jobs/{jid}/cancel", {"by": "carol@corp.example"})[0] == 422
    assert call(h, "POST", f"/v1/jobs/{jid}/publish", {"actor": "dave@corp.example"})[0] == 422
    assert kill(h, True, scope="global", agent_id=None, actor="eve@corp.example")[0] == 422
    assert call(h, "POST", f"/v1/jobs/{jid}/decision", {"decision": "approve", "approver": "u_9c1d"})[0] == 200
    assert call(h, "POST", f"/v1/jobs/{jid}/cancel", {})[0] == 200  # no person named: recorded as "webhook"
    # nothing anyone could be identified by reached the append-only log, or the stored responses
    for f in opaque_cfg.audit_dir.glob("*.jsonl"):
        assert "@" not in f.read_text(encoding="utf-8"), f.name
    rows = [r[0] for r in h._conn.execute("SELECT response FROM inbound_deliveries")]
    assert not any("corp.example" in r or "Alice" in r for r in rows)


def test_a_custom_identity_pattern_is_enforced(opaque_cfg, work_item):
    opaque_cfg.privacy.identity_pattern = r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$"
    h = CommandHandler(opaque_cfg, JobQueue(opaque_cfg.db_path))
    assert call(h, "POST", "/v1/work-items", wi_body(work_item, requester="alice"))[0] == 422
    assert call(h, "POST", "/v1/work-items", wi_body(work_item, requester=str(uuid.uuid4())))[0] == 201
    with pytest.raises(ValidationError):
        PrivacyConfig(identity_pattern="(unclosed")


def test_identities_are_unchanged_when_opaque_mode_is_off(hook_cfg, work_item):
    h = CommandHandler(hook_cfg, JobQueue(hook_cfg.db_path))
    status, resp = call(h, "POST", "/v1/work-items", wi_body(work_item, requester="alice@corp.example"))
    assert status == 201


def test_cli_refuses_names_in_opaque_mode(tmp_path, calc_repo, capsys):
    toml = tmp_path / "gca.toml"
    toml.write_text(f'data_dir = "{(tmp_path / "d").as_posix()}"\n[privacy]\nopaque_identities = true\n')
    sub = ["-c", str(toml), "submit", "--title", "t", "--description", "d", "--repo", str(calc_repo)]
    assert cli_main(sub + ["--requester", "alice@corp.example"]) == 2
    assert cli_main(sub) == 0  # the command's own label "cli" names no one
    jid = capsys.readouterr().out.strip().split()[-1]
    assert cli_main(["-c", str(toml), "approve", jid, "--approver", "Bob Jones"]) == 2
    assert cli_main(["-c", str(toml), "cancel", jid, "--by", "carol@corp.example"]) == 2
    assert cli_main(["-c", str(toml), "kill", "on", "--reason", "x", "--by", "eve@corp.example"]) == 2
    assert cli_main(["-c", str(toml), "approve", jid, "--approver", "u_lead"]) == 0


def test_servicenow_records_people_by_sys_id_in_opaque_mode(config, monkeypatch):
    from unit.test_servicenow import REC, FakeTableAPI, make_source
    monkeypatch.setenv("SN_USER", "svc")
    monkeypatch.setenv("SN_PASSWORD", "pw")
    from gca.sources.servicenow import sync_once
    no_opener = {**REC, "sys_id": "def456", "number": "WI0001002", "opened_by": "", "sys_created_by": "jane.doe"}
    config.privacy = PrivacyConfig(opaque_identities=True)
    q = JobQueue(config.db_path)
    rep = sync_once(config, q, make_source(FakeTableAPI([REC, no_opener])))
    assert rep["errors"] == []
    by_ref = {j.work_item.source_ref: j for j in q.list()}
    assert by_ref["abc123"].work_item.requester == "u1" and by_ref["abc123"].approval.approver == "u2"
    assert by_ref["def456"].work_item.requester == "servicenow"  # never the user name
    for f in config.audit_dir.glob("*.jsonl"):
        text = f.read_text(encoding="utf-8")
        assert "Ashwin" not in text and "Tech Lead" not in text and "jane.doe" not in text
    q.close()


def test_servicenow_keeps_display_names_when_opaque_mode_is_off(config, monkeypatch):
    from unit.test_servicenow import REC, FakeTableAPI, make_source
    monkeypatch.setenv("SN_USER", "svc")
    monkeypatch.setenv("SN_PASSWORD", "pw")
    from gca.sources.servicenow import sync_once
    q = JobQueue(config.db_path)
    sync_once(config, q, make_source(FakeTableAPI([REC])))
    assert q.list()[0].work_item.requester == "Ashwin Prabhu"
    q.close()

