"""SQLite-backed job queue with an enforced state machine.

Guarantees tested:
- idempotent submit (same idempotency_key -> same job)
- transitions only along models.TRANSITIONS
- atomic claim: two workers cannot claim the same job
- stale RUNNING/VERIFYING jobs (dead worker) are recovered to FAILED on `recover_stale`
"""

from __future__ import annotations

import builtins
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

from .models import TRANSITIONS, Approval, Budget, Job, JobStatus, RunResult, WorkItem, now_iso


class InvalidTransition(Exception):
    pass


class JobQueue:
    def __init__(self, db_path: str | Path):
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(str(self.db_path), isolation_level=None, check_same_thread=False)
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA busy_timeout=5000")
        self._conn.execute(
            """CREATE TABLE IF NOT EXISTS jobs (
                   id TEXT PRIMARY KEY,
                   status TEXT NOT NULL,
                   idempotency_key TEXT UNIQUE,
                   created_at TEXT NOT NULL,
                   updated_at TEXT NOT NULL,
                   heartbeat_at TEXT,
                   worker_id TEXT,
                   body TEXT NOT NULL)"""
        )
        self._conn.execute("CREATE INDEX IF NOT EXISTS idx_jobs_status ON jobs(status)")

    def close(self) -> None:
        self._conn.close()

    # ----------------------------------------------------------------- io
    def _save(self, job: Job) -> None:
        job.updated_at = now_iso()
        self._conn.execute(
            "UPDATE jobs SET status=?, updated_at=?, heartbeat_at=?, worker_id=?, body=? WHERE id=?",
            (job.status.value, job.updated_at, job.heartbeat_at, job.worker_id, job.model_dump_json(), job.id),
        )

    def get(self, job_id: str) -> Job:
        row = self._conn.execute("SELECT body FROM jobs WHERE id=?", (job_id,)).fetchone()
        if not row:
            raise KeyError(job_id)
        return Job.model_validate_json(row[0])

    def list(self, status: JobStatus | None = None) -> builtins.list[Job]:
        if status:
            rows = self._conn.execute("SELECT body FROM jobs WHERE status=? ORDER BY created_at", (status.value,))
        else:
            rows = self._conn.execute("SELECT body FROM jobs ORDER BY created_at")
        return [Job.model_validate_json(r[0]) for r in rows]

    # ------------------------------------------------------------ lifecycle
    def submit(self, item: WorkItem, budget: Budget | None = None) -> Job:
        if item.idempotency_key:
            row = self._conn.execute(
                "SELECT body FROM jobs WHERE idempotency_key=?", (item.idempotency_key,)).fetchone()
            if row:
                return Job.model_validate_json(row[0])
        job = Job(work_item=item, budget=budget or Budget())
        self._conn.execute(
            "INSERT INTO jobs (id, status, idempotency_key, created_at, updated_at, body) VALUES (?,?,?,?,?,?)",
            (job.id, job.status.value, item.idempotency_key, job.created_at, job.updated_at, job.model_dump_json()),
        )
        return job

    def transition(self, job_id: str, to: JobStatus, **updates) -> Job:
        job = self.get(job_id)
        if to not in TRANSITIONS[job.status]:
            raise InvalidTransition(f"{job.status.value} -> {to.value} is not allowed for {job_id}")
        job.status = to
        for k, v in updates.items():
            setattr(job, k, v)
        self._save(job)
        return job

    def approve(self, job_id: str, approver: str, note: str = "") -> Job:
        return self.transition(job_id, JobStatus.APPROVED, approval=Approval(approver=approver, note=note))

    def reject(self, job_id: str, approver: str, note: str = "") -> Job:
        return self.transition(job_id, JobStatus.REJECTED, approval=Approval(approver=approver, note=note))

    def request_cancel(self, job_id: str) -> Job:
        """Cancel immediately if not running; otherwise flag it and let the worker stop."""
        job = self.get(job_id)
        if job.status in (JobStatus.SUBMITTED, JobStatus.APPROVED):
            return self.transition(job_id, JobStatus.CANCELLED)
        job.cancel_requested = True
        self._save(job)
        return job

    def cancel_requested(self, job_id: str) -> bool:
        return self.get(job_id).cancel_requested

    def claim_next(self, worker_id: str) -> Job | None:
        """Atomically move one APPROVED job to RUNNING for this worker."""
        cur = self._conn.cursor()
        cur.execute("BEGIN IMMEDIATE")
        try:
            row = cur.execute(
                "SELECT id FROM jobs WHERE status=? ORDER BY created_at LIMIT 1", (JobStatus.APPROVED.value,)
            ).fetchone()
            if not row:
                cur.execute("COMMIT")
                return None
            job = Job.model_validate_json(
                cur.execute("SELECT body FROM jobs WHERE id=?", (row[0],)).fetchone()[0])
            job.status = JobStatus.RUNNING
            job.worker_id = worker_id
            job.attempts += 1
            job.heartbeat_at = now_iso()
            job.updated_at = now_iso()
            cur.execute(
                "UPDATE jobs SET status=?, updated_at=?, heartbeat_at=?, worker_id=?, body=? WHERE id=? AND status=?",
                (job.status.value, job.updated_at, job.heartbeat_at, job.worker_id, job.model_dump_json(), job.id,
                 JobStatus.APPROVED.value),
            )
            cur.execute("COMMIT")
            return job
        except Exception:
            cur.execute("ROLLBACK")
            raise

    def heartbeat(self, job_id: str) -> None:
        self._conn.execute("UPDATE jobs SET heartbeat_at=? WHERE id=?", (now_iso(), job_id))

    def finish(self, job_id: str, status: JobStatus, result: RunResult | None = None, error: str | None = None) -> Job:
        return self.transition(job_id, status, result=result, error=error)

    def recover_stale(self, stale_after_seconds: int) -> builtins.list[Job]:
        """Mark RUNNING/VERIFYING jobs whose worker stopped heartbeating as FAILED."""
        cutoff = (datetime.now(timezone.utc) - timedelta(seconds=stale_after_seconds)).isoformat()
        rows = self._conn.execute(
            "SELECT id FROM jobs WHERE status IN (?,?) AND (heartbeat_at IS NULL OR heartbeat_at < ?)",
            (JobStatus.RUNNING.value, JobStatus.VERIFYING.value, cutoff),
        ).fetchall()
        out = []
        for (jid,) in rows:
            out.append(self.finish(jid, JobStatus.FAILED, error="worker_crashed_or_stale_heartbeat"))
        return out
