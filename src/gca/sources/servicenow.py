"""ServiceNow Table API adapter (experimental — not exercised by the automated tests).

Expected table (scoped app, e.g. x_gca_work_item):
    number, short_description, description, acceptance_criteria (newline list), repo_path,
    base_ref, test_command, allowed_paths, state (requested|approved|running|succeeded|failed),
    approver, gca_job_id, gca_branch, gca_report, gca_audit_hash

Approval happens in ServiceNow (state -> approved); the runner never approves anything itself.
"""

from __future__ import annotations

import os
import shlex
from typing import Any

import httpx

from ..models import Job, WorkItem


class ServiceNowSource:
    def __init__(self, instance_url: str, table: str, *, user_env: str = "SN_USER", password_env: str = "SN_PASSWORD",
                 timeout: float = 30.0):
        self.base = instance_url.rstrip("/")
        self.table = table
        self.auth = (os.environ.get(user_env, ""), os.environ.get(password_env, ""))
        self.timeout = timeout

    def _url(self, sys_id: str | None = None) -> str:
        return f"{self.base}/api/now/table/{self.table}" + (f"/{sys_id}" if sys_id else "")

    def fetch_approved(self, limit: int = 10) -> list[dict[str, Any]]:
        params = {"sysparm_query": "state=approved^gca_job_idISEMPTY", "sysparm_limit": str(limit)}
        with httpx.Client(timeout=self.timeout, auth=self.auth) as c:
            r = c.get(self._url(), params=params, headers={"Accept": "application/json"})
            r.raise_for_status()
            return r.json().get("result", [])

    @staticmethod
    def to_work_item(rec: dict[str, Any]) -> WorkItem:
        crit = [c.strip() for c in (rec.get("acceptance_criteria") or "").splitlines() if c.strip()]
        allowed = [p.strip() for p in (rec.get("allowed_paths") or "").split(",") if p.strip()]
        test_cmd = shlex.split(rec.get("test_command") or "python -m pytest -q")
        return WorkItem(
            title=rec.get("short_description") or rec.get("number", "ServiceNow work item"),
            description=rec.get("description") or "", acceptance_criteria=crit,
            repo_path=rec["repo_path"], base_ref=rec.get("base_ref") or "HEAD", test_command=test_cmd,
            allowed_paths=allowed, requester=rec.get("opened_by", {}).get("value", "servicenow") if isinstance(rec.get("opened_by"), dict) else "servicenow",
            source="servicenow", source_ref=rec["sys_id"], idempotency_key=f"servicenow:{rec['sys_id']}",
        )

    def write_back(self, rec_sys_id: str, job: Job, audit_hash: str | None = None) -> None:
        body: dict[str, Any] = {"state": job.status.value, "gca_job_id": job.id}
        if job.result:
            body["gca_branch"] = job.result.branch or ""
            body["gca_report"] = job.result.report_markdown[:60000]
        if audit_hash:
            body["gca_audit_hash"] = audit_hash
        with httpx.Client(timeout=self.timeout, auth=self.auth) as c:
            r = c.patch(self._url(rec_sys_id), json=body, headers={"Accept": "application/json"})
            r.raise_for_status()
