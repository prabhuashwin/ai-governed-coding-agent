"""ServiceNow Table API adapter.

Covered by tests against a mocked Table API; not yet validated against a live instance (needs a PDI).

Expected table (scoped app `x_gca_work_item` with field_prefix "" — or a global custom table
`u_gca_work_item` with field_prefix "u_"; see scripts/servicenow_bootstrap.py which creates the latter):
    short_description, description, acceptance_criteria (one per line), repo_path, base_ref,
    test_command, allowed_paths (comma-separated), state, approver,
    gca_job_id, gca_branch, gca_status, gca_report, gca_error, gca_audit_hash
    (+ number / opened_by if the table has them)

State contract:
    requested  -> engineer created it (ignored by gca)
    approved   -> approver set it in ServiceNow; gca picks it up, records the approval locally and queues it
    running / verifying / succeeded / failed / cancelled -> written back by gca
Approval never happens in gca; it only mirrors ServiceNow's decision.
"""

from __future__ import annotations

import os
import re
from typing import Any

import httpx

from ..audit import AuditLog
from ..cmdline import split_command
from ..config import Config, ServiceNowConfig
from ..models import TERMINAL_STATES, Job, JobStatus, WorkItem
from ..policy import check_repo_allowed
from ..queue import JobQueue

SYS_ID_PATTERN = r"^[0-9a-f]{32}$"  # a ServiceNow record id; what people are recorded as in opaque mode


class ServiceNowSource:
    def __init__(self, cfg: ServiceNowConfig, *, transport: httpx.BaseTransport | None = None):
        instance = cfg.instance_url or os.environ.get("SN_INSTANCE_URL", "")
        if not instance:
            raise ValueError("[servicenow] instance_url is not configured (or set SN_INSTANCE_URL)")
        self.cfg = cfg
        self.base = instance.rstrip("/")
        self.table = cfg.table
        self.auth = (os.environ.get(cfg.user_env, ""), os.environ.get(cfg.password_env, ""))
        self._transport = transport
        self.p = cfg.field_prefix or ""

    def f(self, name: str) -> str:
        """Field name with the configured prefix (sys_* fields are never prefixed)."""
        return name if name.startswith("sys_") else f"{self.p}{name}"

    def g(self, rec: dict[str, Any], name: str, default: Any = "") -> Any:
        return rec.get(self.f(name), default)

    def _client(self) -> httpx.Client:
        return httpx.Client(timeout=self.cfg.timeout_seconds, auth=self.auth, transport=self._transport,
                            headers={"Accept": "application/json", "Content-Type": "application/json"})

    def _url(self, sys_id: str | None = None) -> str:
        return f"{self.base}/api/now/table/{self.table}" + (f"/{sys_id}" if sys_id else "")

    # ------------------------------------------------------------------ read
    def fetch_approved(self, limit: int = 10) -> list[dict[str, Any]]:
        params = {"sysparm_query": f"{self.f('state')}=approved^{self.f('gca_job_id')}ISEMPTY^ORDERBYsys_created_on",
                  "sysparm_limit": str(limit), "sysparm_display_value": "false"}
        with self._client() as c:
            r = c.get(self._url(), params=params)
            r.raise_for_status()
            return r.json().get("result", [])

    def fetch_in_flight(self, limit: int = 100) -> list[dict[str, Any]]:
        params = {"sysparm_query": f"{self.f('gca_job_id')}ISNOTEMPTY^{self.f('state')}INapproved,running,verifying",
                  "sysparm_limit": str(limit),
                  "sysparm_fields": ",".join(["sys_id", self.f("number"), self.f("gca_job_id"), self.f("state")])}
        with self._client() as c:
            r = c.get(self._url(), params=params)
            r.raise_for_status()
            return r.json().get("result", [])

    @staticmethod
    def _ref(v: Any, *, opaque: bool = False) -> str:
        """A reference field's value. With `opaque`, only the sys_id, never a display value (a person's name)."""
        if isinstance(v, dict):
            return (v.get("value") or "") if opaque else (v.get("display_value") or v.get("value") or "")
        return v or ""

    def to_work_item(self, rec: dict[str, Any], *, opaque: bool = False) -> WorkItem:
        g = self.g
        crit = [c.strip() for c in (g(rec, "acceptance_criteria") or "").splitlines() if c.strip()]
        allowed = [p.strip() for p in (g(rec, "allowed_paths") or "").split(",") if p.strip()]
        test_cmd = split_command(g(rec, "test_command") or "python -m pytest -q")
        label = g(rec, "number") or rec.get("sys_id")
        if not g(rec, "repo_path"):
            raise ValueError(f"{label}: repo_path is empty")
        # sys_created_by is a user name: never used when people must be recorded by opaque id
        requester = self._ref(g(rec, "opened_by"), opaque=opaque) or (None if opaque else rec.get("sys_created_by")) \
            or "servicenow"
        return WorkItem(
            title=g(rec, "short_description") or g(rec, "number") or "ServiceNow work item",
            description=g(rec, "description") or "", acceptance_criteria=crit,
            repo_path=g(rec, "repo_path"), base_ref=g(rec, "base_ref") or "HEAD", test_command=test_cmd,
            allowed_paths=allowed, requester=requester,
            source="servicenow", source_ref=rec["sys_id"], idempotency_key=f"servicenow:{rec['sys_id']}",
        )

    # ----------------------------------------------------------------- write
    def write_back(self, sys_id: str, job: Job, audit_hash: str | None = None) -> None:
        body: dict[str, Any] = {self.f("state"): job.status.value, self.f("gca_job_id"): job.id,
                                self.f("gca_status"): job.status.value, self.f("gca_error"): job.error or ""}
        if job.result:
            body[self.f("gca_branch")] = job.result.branch or ""
            body[self.f("gca_report")] = (job.result.report_markdown or "")[:60000]
        if audit_hash:
            body[self.f("gca_audit_hash")] = audit_hash
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
            opaque = cfg.privacy.opaque_identities
            item = source.to_work_item(rec, opaque=opaque)
            item.repo_path = check_repo_allowed(cfg.policy.allowed_repos, item.repo_path)
            approver = source._ref(source.g(rec, "approver"), opaque=opaque) or "servicenow"
            for who, field in ((item.requester, "opened_by"), (approver, "approver")):
                # people are recorded by sys_id only; "servicenow" means no person is on the record. A plain text
                # field holding a user name is refused (the value is not echoed into the error)
                if opaque and who != "servicenow" and not re.fullmatch(SYS_ID_PATTERN, who):
                    raise ValueError(f"{field} must be a sys_user sys_id when opaque identities are on "
                                     "(a reference to sys_user, not a name or user name)")
            job = queue.submit(item, cfg.budget)
            audit = AuditLog(cfg.audit_dir, job.id, redactions=cfg.policy.canary_strings)
            if job.status == JobStatus.SUBMITTED:
                audit.record(item.requester, "submitted", work_item=item.model_dump(), source="servicenow",
                             record=source.g(rec, "number"))
                job = queue.approve(job.id, approver, note=f"approved in ServiceNow ({source.g(rec, 'number')})")
                audit.record(approver, "approved", note="mirrored from ServiceNow")
            source.write_back(rec["sys_id"], job)
            report["queued"].append(f"{source.g(rec, 'number') or rec['sys_id']}->{job.id}")
        except Exception as e:  # noqa: BLE001 - one bad record must not stop the cycle
            report["errors"].append(f"{source.g(rec, 'number') or rec.get('sys_id')}: {e}")

    for rec in source.fetch_in_flight():
        label = source.g(rec, "number") or rec.get("sys_id")
        try:
            job = queue.get(source.g(rec, "gca_job_id"))
        except KeyError:
            report["errors"].append(f"{label}: unknown job {source.g(rec, 'gca_job_id')}")
            continue
        if job.status.value != source.g(rec, "state"):
            audit_hash = None
            if job.status in TERMINAL_STATES:
                events = AuditLog(cfg.audit_dir, job.id).events()
                audit_hash = events[-1].hash if events else None
            try:
                source.write_back(rec["sys_id"], job, audit_hash)
                report["updated"].append(f"{label}:{job.status.value}")
            except Exception as e:  # noqa: BLE001
                report["errors"].append(f"{label}: {e}")
    return report
