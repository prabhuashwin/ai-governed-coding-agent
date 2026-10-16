"""ServiceNow Table API adapter.

Covered by tests against a mocked Table API; not yet validated against a live instance (needs a PDI).

Expected scoped table (e.g. x_gca_work_item):
    number, short_description, description, acceptance_criteria (one per line), repo_path, base_ref,
    test_command, allowed_paths (comma-separated), state, approver,
    gca_job_id, gca_branch, gca_status, gca_report, gca_error, gca_audit_hash

State contract:
    requested  -> engineer created it (ignored by gca)
    approved   -> approver set it in ServiceNow; gca picks it up, records the approval locally and queues it
    running / succeeded / failed / cancelled -> written back by gca
Approval never happens in gca; it only mirrors ServiceNow's decision.
"""

from __future__ import annotations

import os
import shlex
from typing import Any

import httpx

from ..audit import AuditLog
from ..config import Config, ServiceNowConfig
from ..models import TERMINAL_STATES, Job, JobStatus, WorkItem
from ..queue import JobQueue


class ServiceNowSource:
    def __init__(self, cfg: ServiceNowConfig, *, transport: httpx.BaseTransport | None = None):
        if not cfg.instance_url:
            raise ValueError("[servicenow] instance_url is not configured")
        self.cfg = cfg
        self.base = cfg.instance_url.rstrip("/")
        self.table = cfg.table
        self.auth = (os.environ.get(cfg.user_env, ""), os.environ.get(cfg.password_env, ""))
        self._transport = transport

    def _client(self) -> httpx.Client:
        return httpx.Client(timeout=self.cfg.timeout_seconds, auth=self.auth, transport=self._transport,
                            headers={"Accept": "application/json", "Content-Type": "application/json"})

    def _url(self, sys_id: str | None = None) -> str:
        return f"{self.base}/api/now/table/{self.table}" + (f"/{sys_id}" if sys_id else "")

    # ------------------------------------------------------------------ read
    def fetch_approved(self, limit: int = 10) -> list[dict[str, Any]]:
        params = {"sysparm_query": "state=approved^gca_job_idISEMPTY^ORDERBYsys_created_on",
                  "sysparm_limit": str(limit), "sysparm_display_value": "false"}
        with self._client() as c:
            r = c.get(self._url(), params=params)
            r.raise_for_status()
            return r.json().get("result", [])

    def fetch_in_flight(self, limit: int = 100) -> list[dict[str, Any]]:
        params = {"sysparm_query": "gca_job_idISNOTEMPTY^stateINapproved,running",
                  "sysparm_limit": str(limit), "sysparm_fields": "sys_id,number,gca_job_id,state"}
        with self._client() as c:
            r = c.get(self._url(), params=params)
            r.raise_for_status()
            return r.json().get("result", [])

    @staticmethod
    def _ref(v: Any) -> str:
        return v.get("display_value") or v.get("value") or "" if isinstance(v, dict) else (v or "")

    def to_work_item(self, rec: dict[str, Any]) -> WorkItem:
        crit = [c.strip() for c in (rec.get("acceptance_criteria") or "").splitlines() if c.strip()]
        allowed = [p.strip() for p in (rec.get("allowed_paths") or "").split(",") if p.strip()]
        test_cmd = shlex.split(rec.get("test_command") or "python -m pytest -q")
        if not rec.get("repo_path"):
            raise ValueError(f"{rec.get('number', rec.get('sys_id'))}: repo_path is empty")
        return WorkItem(
            title=rec.get("short_description") or rec.get("number") or "ServiceNow work item",
            description=rec.get("description") or "", acceptance_criteria=crit,
            repo_path=rec["repo_path"], base_ref=rec.get("base_ref") or "HEAD", test_command=test_cmd,
            allowed_paths=allowed, requester=self._ref(rec.get("opened_by")) or "servicenow",
            source="servicenow", source_ref=rec["sys_id"], idempotency_key=f"servicenow:{rec['sys_id']}",
        )

    # ----------------------------------------------------------------- write
    def write_back(self, sys_id: str, job: Job, audit_hash: str | None = None) -> None:
        body: dict[str, Any] = {"state": job.status.value, "gca_job_id": job.id, "gca_status": job.status.value,
                                "gca_error": job.error or ""}
        if job.result:
            body["gca_branch"] = job.result.branch or ""
            body["gca_report"] = (job.result.report_markdown or "")[:60000]
        if audit_hash:
            body["gca_audit_hash"] = audit_hash
        with self._client() as c:
            r = c.patch(self._url(sys_id), json=body)
            r.raise_for_status()


def sync_once(cfg: Config, queue: JobQueue, source: ServiceNowSource) -> dict[str, list[str]]:
    """One polling cycle. Returns what happened, for logging/tests.

    1. New approved records -> submit (idempotent) + mirror the approval locally + write job id back.
    2. In-flight records -> write back the current local status when it changed, with the audit tail hash.
    """
    report: dict[str, list[str]] = {"queued": [], "updated": [], "errors": []}

    for rec in source.fetch_approved():
        try:
            item = source.to_work_item(rec)
            job = queue.submit(item, cfg.budget)
            audit = AuditLog(cfg.audit_dir, job.id, redactions=cfg.policy.canary_strings)
            if job.status == JobStatus.SUBMITTED:
                audit.record(item.requester, "submitted", work_item=item.model_dump(), source="servicenow",
                             record=rec.get("number"))
                approver = source._ref(rec.get("approver")) or "servicenow"
                job = queue.approve(job.id, approver, note=f"approved in ServiceNow ({rec.get('number', '')})")
                audit.record(approver, "approved", note="mirrored from ServiceNow")
            source.write_back(rec["sys_id"], job)
            report["queued"].append(f"{rec.get('number', rec['sys_id'])}->{job.id}")
        except Exception as e:  # noqa: BLE001 - one bad record must not stop the cycle
            report["errors"].append(f"{rec.get('number', rec.get('sys_id'))}: {e}")

    for rec in source.fetch_in_flight():
        try:
            job = queue.get(rec["gca_job_id"])
        except KeyError:
            report["errors"].append(f"{rec.get('number')}: unknown job {rec.get('gca_job_id')}")
            continue
        if job.status.value != rec.get("state"):
            audit_hash = None
            if job.status in TERMINAL_STATES:
                events = AuditLog(cfg.audit_dir, job.id).events()
                audit_hash = events[-1].hash if events else None
            try:
                source.write_back(rec["sys_id"], job, audit_hash)
                report["updated"].append(f"{rec.get('number')}:{job.status.value}")
            except Exception as e:  # noqa: BLE001
                report["errors"].append(f"{rec.get('number')}: {e}")
    return report
