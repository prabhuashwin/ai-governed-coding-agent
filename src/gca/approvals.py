"""Approval deadlines: how long a job may wait for a human decision, and what happens when the wait is over.

The operator sets the longest wait (`[approval] timeout_seconds`); a submission may ask for a shorter one
(`approval_timeout_seconds`, e.g. the approving platform's own SLA) but never a longer one. A job still awaiting a
decision when its window closes is cancelled with error "approval_expired", recorded in its audit log (and so sent
as an `approval.expired` event), and any decision that arrives afterwards is refused.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

from .audit import AuditLog
from .config import MAX_APPROVAL_TIMEOUT_SECONDS, Config
from .models import Job
from .queue import JobQueue


def approval_deadline(cfg: Config, requested: Any = None) -> str | None:
    """The absolute deadline (UTC ISO 8601) for a job submitted now, or None to wait indefinitely."""
    if requested is not None and (isinstance(requested, bool) or not isinstance(requested, int)
                                  or not 0 < requested <= MAX_APPROVAL_TIMEOUT_SECONDS):
        raise ValueError(f"approval_timeout_seconds must be a whole number of seconds from 1 to "
                         f"{MAX_APPROVAL_TIMEOUT_SECONDS}")
    windows = [w for w in (requested, cfg.approval.timeout_seconds) if w is not None]
    if not windows:
        return None
    return (datetime.now(timezone.utc) + timedelta(seconds=min(windows))).isoformat()


def expire_overdue(cfg: Config, queue: JobQueue, *, job_id: str | None = None) -> list[Job]:
    """Cancel jobs whose approval window has closed (all of them, or just `job_id`) and record each expiry once.

    Recording is at least once: an expiry whose audit entry could not be written (a crash, a damaged log) stays
    marked and is recorded by a later sweep; one failure never holds up the others. Returns the jobs whose
    expiry this call recorded."""
    queue.expire_overdue_approvals(job_id=job_id)
    recorded: list[Job] = []
    for jid in queue.unreported_expiries(job_id=job_id):
        job = queue.get(jid)
        if job.error == "approval_expired":
            try:
                AuditLog(cfg.audit_dir, job.id, redactions=cfg.policy.canary_strings).record(
                    "gca", "approval_expired", expires_at=job.approval_expires_at, agent_id=job.work_item.agent_id)
            except Exception:  # noqa: BLE001 - left marked; the next sweep records it
                continue
            recorded.append(job)
        queue.settle_expiry(jid)  # recorded, or a job cancelled for another reason: nothing (more) to report
    return recorded
