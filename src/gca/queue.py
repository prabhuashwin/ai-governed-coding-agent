"""SQLite-backed job queue with an enforced state machine.

Guarantees tested:
- idempotent submit (same idempotency_key -> same job)
- transitions only along models.TRANSITIONS
- atomic claim: two workers cannot claim the same job
- stale RUNNING/VERIFYING jobs (dead worker) are recovered to FAILED on `recover_stale`
- a job past its approval deadline cannot be approved, and is swept to CANCELLED ("approval_expired")
- nothing of a stopped agent is claimed, and its running job never ends SUCCEEDED
"""

from __future__ import annotations

import builtins
import json
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path

from .models import (
    TRANSITIONS,
    Approval,
    Budget,
    Job,
    JobStatus,
    Restrictions,
    RunResult,
    WorkItem,
    add_column_if_missing,
    now_iso,
    open_db,
    set_wal,
)


class InvalidTransition(Exception):
    pass


class ApprovalExpired(InvalidTransition):
    """A decision arrived after the job's approval window closed."""


class _Unchanged(Exception):
    """Abort an `_update` without writing (the row no longer matches what the caller expected)."""


GLOBAL_CONTROL = "kill:global"
AGENT_CONTROL_PREFIX = "kill:agent:"


def _control_name(agent_id: str | None) -> str:
    """The `controls` row of the instance switch (agent_id None) or of one agent's switch."""
    return GLOBAL_CONTROL if agent_id is None else AGENT_CONTROL_PREFIX + agent_id


def _check_window(job: Job) -> None:
    if job.status == JobStatus.SUBMITTED and job.approval_expires_at and job.approval_expires_at <= now_iso():
        raise ApprovalExpired(f"approval expired: the window for {job.id} closed at {job.approval_expires_at}")


def _serialised(fn):
    """Run a JobQueue method under the queue's connection lock (re-entrant, so locked methods may nest)."""
    def wrapper(self, *a, **kw):
        with self._txn:
            return fn(self, *a, **kw)
    wrapper.__name__, wrapper.__doc__ = fn.__name__, fn.__doc__
    return wrapper


class JobQueue:
    def __init__(self, db_path: str | Path):
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = open_db(self, str(self.db_path))
        # Threads share this connection. SQLite runs a statement issued while another thread's BEGIN IMMEDIATE is
        # open *inside* that transaction, so a later ROLLBACK would undo it. Every operation therefore takes
        # this re-entrant lock, not only the explicit transactions.
        self._txn = threading.RLock()
        set_wal(self._conn)
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
        self._conn.execute(
            """CREATE TABLE IF NOT EXISTS controls (
                   name TEXT PRIMARY KEY, value TEXT NOT NULL, reason TEXT, set_by TEXT, set_at TEXT NOT NULL)"""
        )
        add_column_if_missing(self._conn, "controls", "delivery_id", "TEXT")  # databases from before 0.2.0
        # 0.3.0: both are copies of fields in `body`, fixed at submission, so claims and the approval sweep can
        # filter in SQL. Rows from older databases have NULL: no agent, no deadline.
        add_column_if_missing(self._conn, "jobs", "agent_id", "TEXT")
        add_column_if_missing(self._conn, "jobs", "approval_expires_at", "TEXT")
        self._conn.execute("CREATE INDEX IF NOT EXISTS idx_jobs_agent ON jobs(agent_id)")
        self._conn.execute("CREATE INDEX IF NOT EXISTS idx_jobs_expiry ON jobs(status, approval_expires_at)")

    def close(self) -> None:
        self._conn.close()

    # ----------------------------------------------------------------- io
    def _save(self, job: Job) -> None:
        job.updated_at = now_iso()
        self._conn.execute(
            "UPDATE jobs SET status=?, updated_at=?, heartbeat_at=?, worker_id=?, body=? WHERE id=?",
            (job.status.value, job.updated_at, job.heartbeat_at, job.worker_id, job.model_dump_json(), job.id),
        )

    @_serialised
    def get(self, job_id: str) -> Job:
        row = self._conn.execute("SELECT body FROM jobs WHERE id=?", (job_id,)).fetchone()
        if not row:
            raise KeyError(job_id)
        return Job.model_validate_json(row[0])

    @_serialised
    def list(self, status: JobStatus | None = None) -> builtins.list[Job]:
        if status:
            rows = self._conn.execute("SELECT body FROM jobs WHERE status=? ORDER BY created_at", (status.value,))
        else:
            rows = self._conn.execute("SELECT body FROM jobs ORDER BY created_at")
        return [Job.model_validate_json(r[0]) for r in rows]

    # ------------------------------------------------------------ lifecycle
    @_serialised
    def submit(self, item: WorkItem, budget: Budget | None = None, *, approval_expires_at: str | None = None) -> Job:
        """Queue a work item. A repeat with the same idempotency key returns the first job unchanged (including
        its approval deadline, which runs from the first submission)."""
        if item.idempotency_key:
            row = self._conn.execute(
                "SELECT body FROM jobs WHERE idempotency_key=?", (item.idempotency_key,)).fetchone()
            if row:
                return Job.model_validate_json(row[0])
        job = Job(work_item=item, budget=budget or Budget(), approval_expires_at=approval_expires_at)
        self._conn.execute(
            "INSERT INTO jobs (id, status, idempotency_key, created_at, updated_at, body, agent_id, approval_expires_at) "
            "VALUES (?,?,?,?,?,?,?,?)",
            (job.id, job.status.value, item.idempotency_key, job.created_at, job.updated_at, job.model_dump_json(),
             item.agent_id, approval_expires_at),
        )
        return job

    def _update(self, job_id: str, mutate) -> Job:
        """Read-modify-write one job inside a write transaction, so concurrent writers (a worker finishing a job,
        a `gca cancel` from another process) serialise instead of overwriting each other's row."""
        with self._txn:
            cur = self._conn.cursor()
            cur.execute("BEGIN IMMEDIATE")
            try:
                row = cur.execute("SELECT body FROM jobs WHERE id=?", (job_id,)).fetchone()
                if not row:
                    raise KeyError(job_id)
                job = Job.model_validate_json(row[0])
                mutate(job)
                job.updated_at = now_iso()
                cur.execute(
                    "UPDATE jobs SET status=?, updated_at=?, heartbeat_at=?, worker_id=?, body=? WHERE id=?",
                    (job.status.value, job.updated_at, job.heartbeat_at, job.worker_id, job.model_dump_json(), job.id),
                )
                cur.execute("COMMIT")
                return job
            except Exception:
                cur.execute("ROLLBACK")
                raise

    def transition(self, job_id: str, to: JobStatus, **updates) -> Job:
        def mutate(job: Job) -> None:
            if to not in TRANSITIONS[job.status]:
                raise InvalidTransition(f"{job.status.value} -> {to.value} is not allowed for {job_id}")
            job.status = to
            for k, v in updates.items():
                setattr(job, k, v)
        return self._update(job_id, mutate)

    def approve(self, job_id: str, approver: str, note: str = "", *, conditions: builtins.list[str] | None = None,
                restrictions: Restrictions | None = None, operator_commands: builtins.list[str] | None = None) -> Job:
        """Approve, applying any enforceable restrictions to the job in the same transaction."""
        def mutate(job: Job) -> None:
            _check_window(job)  # read inside the write transaction: a decision is either in time or refused
            if JobStatus.APPROVED not in TRANSITIONS[job.status]:
                raise InvalidTransition(f"{job.status.value} -> approved is not allowed for {job_id}")
            applied: builtins.list[str] = []
            if restrictions is not None:
                applied = restrictions.apply(job, operator_commands or [])
            job.status = JobStatus.APPROVED
            job.approval = Approval(approver=approver, note=note, conditions=list(conditions or []),
                                    restrictions=restrictions)
            if applied:
                job.approval.note = (job.approval.note + "\nEnforced: " + "; ".join(applied)).strip()
        return self._update(job_id, mutate)

    def reject(self, job_id: str, approver: str, note: str = "") -> Job:
        def mutate(job: Job) -> None:
            _check_window(job)
            if JobStatus.REJECTED not in TRANSITIONS[job.status]:
                raise InvalidTransition(f"{job.status.value} -> rejected is not allowed for {job_id}")
            job.status = JobStatus.REJECTED
            job.approval = Approval(approver=approver, note=note)
        return self._update(job_id, mutate)

    def expire_overdue_approvals(self, *, job_id: str | None = None, now: str | None = None) -> builtins.list[Job]:
        """Cancel every job still awaiting approval whose deadline has passed (error "approval_expired"); return
        the jobs this call expired. Each is re-checked inside its own write transaction, so a decision that
        committed first wins and the job is left as decided."""
        now = now or now_iso()
        sql = ("SELECT id FROM jobs WHERE status=? AND approval_expires_at IS NOT NULL AND approval_expires_at <= ?"
               + (" AND id=?" if job_id else ""))
        params: builtins.list[str] = [JobStatus.SUBMITTED.value, now] + ([job_id] if job_id else [])
        with self._txn:
            ids = [r[0] for r in self._conn.execute(sql, params).fetchall()]
        expired: builtins.list[Job] = []
        for jid in ids:
            def mutate(job: Job) -> None:
                if job.status != JobStatus.SUBMITTED or not job.approval_expires_at or job.approval_expires_at > now:
                    raise _Unchanged
                job.status, job.error = JobStatus.CANCELLED, "approval_expired"
            try:
                expired.append(self._update(jid, mutate))
            except _Unchanged:
                pass
        return expired

    def request_cancel(self, job_id: str) -> Job:
        """Cancel immediately if not running; otherwise flag it and let the worker stop.

        Done under the write lock against the row as it is *now*: a job that finished since the caller last
        looked is left finished, never written back to its earlier state."""
        def mutate(job: Job) -> None:
            if job.status in (JobStatus.SUBMITTED, JobStatus.APPROVED):
                job.status = JobStatus.CANCELLED
            elif job.status in (JobStatus.RUNNING, JobStatus.VERIFYING):
                job.cancel_requested = True
            # terminal states: nothing to do
        return self._update(job_id, mutate)

    def cancel_requested(self, job_id: str) -> bool:
        return self.get(job_id).cancel_requested

    def claim_next(self, worker_id: str) -> Job | None:
        """Atomically move one APPROVED job to RUNNING for this worker. Nothing is claimed while the instance is
        killed, and no job of a stopped agent is claimed (it waits, approved, until that agent is released)."""
        if self.kill_switch():
            return None
        with self._txn:
            return self._claim(worker_id)

    def _claim(self, worker_id: str) -> Job | None:
        cur = self._conn.cursor()
        cur.execute("BEGIN IMMEDIATE")
        try:
            row = cur.execute(
                "SELECT id FROM jobs WHERE status=? AND (agent_id IS NULL OR NOT EXISTS ("
                "SELECT 1 FROM controls c WHERE c.name = ? || jobs.agent_id AND c.value = 'on')) "
                "ORDER BY created_at LIMIT 1", (JobStatus.APPROVED.value, AGENT_CONTROL_PREFIX)
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

    # ------------------------------------------------------------ kill switch
    # Two kinds of switch, each a row in `controls`: the instance switch ("kill:global"; scopes global and tenant)
    # and one per agent ("kill:agent:<agent_id>"). Both stop running work and hold queued work while engaged.
    @_serialised
    def set_kill_switch(self, enabled: bool, *, reason: str = "", actor: str = "unknown",
                        delivery_id: str | None = None, agent_id: str | None = None) -> None:
        self._conn.execute(
            "INSERT INTO controls (name, value, reason, set_by, set_at, delivery_id) VALUES (?, ?, ?, ?, ?, ?) "
            "ON CONFLICT(name) DO UPDATE SET value=excluded.value, reason=excluded.reason, "
            "set_by=excluded.set_by, set_at=excluded.set_at, delivery_id=excluded.delivery_id",
            (_control_name(agent_id), "on" if enabled else "off", reason, actor, now_iso(), delivery_id))

    @_serialised
    def apply_kill_switch_delivery(self, delivery_id: str, first_received_at: str, enabled: bool, *, reason: str,
                                   actor: str, response: str, agent_id: str | None = None) -> str:
        """Apply a kill-switch command that arrived as webhook delivery `delivery_id`, atomically with its outcome.

        One write transaction covers: the supersession check (has the switch been changed since this delivery was
        *first* received?), the control update, and recording the delivery as done in `inbound_deliveries`. So a
        newer change cannot slip in between the check and the update, and a crash or audit failure after the
        commit cannot leave the delivery looking never-applied. Returns "applied", "already_applied" or
        "superseded"; the latter two change nothing.
        """
        name = _control_name(agent_id)  # supersession is per switch: an agent's switch never races the instance's
        cur = self._conn.cursor()
        cur.execute("BEGIN IMMEDIATE")
        try:
            row = cur.execute(
                "SELECT value, set_at, delivery_id FROM controls WHERE name=?", (name,)).fetchone()
            if row and row[2] == delivery_id:
                outcome = "already_applied"
            elif row and (row[1] or "") > first_received_at:
                outcome = "superseded"
            else:
                cur.execute(
                    "INSERT INTO controls (name, value, reason, set_by, set_at, delivery_id) "
                    "VALUES (?, ?, ?, ?, ?, ?) ON CONFLICT(name) DO UPDATE SET value=excluded.value, "
                    "reason=excluded.reason, set_by=excluded.set_by, set_at=excluded.set_at, "
                    "delivery_id=excluded.delivery_id",
                    (name, "on" if enabled else "off", reason, actor, now_iso(), delivery_id))
                outcome = "applied"
            # Every outcome records a valid JSON response: a replay of this delivery id is answered from the
            # row, so an empty body here would turn the second retry into a server error.
            if outcome == "superseded":
                status, body = 409, json.dumps({"error": "superseded: the kill switch was changed after this "
                                                         "delivery was first received", "delivery_id": delivery_id})
            else:
                status, body = 200, response
            cur.execute("UPDATE inbound_deliveries SET status=?, response=? WHERE id=?", (status, body, delivery_id))
            cur.execute("COMMIT")
            return outcome
        except Exception:
            cur.execute("ROLLBACK")
            raise

    @_serialised
    def kill_switch_record(self, agent_id: str | None = None) -> dict[str, str | None] | None:
        """A switch's control row as last written (engaged or not), or None if never set. Default: the instance's."""
        row = self._conn.execute(
            "SELECT value, reason, set_by, set_at, delivery_id FROM controls WHERE name=?",
            (_control_name(agent_id),)).fetchone()
        if not row:
            return None
        return {"value": row[0], "reason": row[1] or "", "set_by": row[2] or "", "set_at": row[3], "delivery_id": row[4]}

    @_serialised
    def kill_switch(self, agent_id: str | None = None) -> dict[str, str] | None:
        """Return the switch's record if engaged, else None. Default: the instance switch."""
        rec = self.kill_switch_record(agent_id)
        if rec and rec["value"] == "on":
            return {"reason": rec["reason"] or "", "set_by": rec["set_by"] or "", "set_at": rec["set_at"] or ""}
        return None

    @_serialised
    def stopped_agents(self) -> builtins.list[dict[str, str]]:
        """Every agent whose switch is engaged, with who stopped it, when and why."""
        rows = self._conn.execute(
            "SELECT name, reason, set_by, set_at FROM controls WHERE value='on' AND substr(name, 1, ?) = ? ORDER BY name",
            (len(AGENT_CONTROL_PREFIX), AGENT_CONTROL_PREFIX)).fetchall()
        return [{"agent_id": r[0][len(AGENT_CONTROL_PREFIX):], "reason": r[1] or "", "set_by": r[2] or "",
                 "set_at": r[3] or ""} for r in rows]

    @_serialised
    def stop_cause(self, agent_id: str | None = None) -> str | None:
        """Why work of this agent must stop now: "kill_switch" (instance), "agent_stopped" (its own switch) or None.
        One read, so a worker polling this sees both switches at the same moment."""
        names = [GLOBAL_CONTROL] + ([_control_name(agent_id)] if agent_id else [])
        rows = dict(self._conn.execute(
            f"SELECT name, value FROM controls WHERE name IN ({','.join('?' * len(names))})", names).fetchall())
        if rows.get(GLOBAL_CONTROL) == "on":
            return "kill_switch"
        if agent_id and rows.get(_control_name(agent_id)) == "on":
            return "agent_stopped"
        return None

    @_serialised
    def heartbeat(self, job_id: str) -> None:
        self._conn.execute("UPDATE jobs SET heartbeat_at=? WHERE id=?", (now_iso(), job_id))

    def finish(self, job_id: str, status: JobStatus, result: RunResult | None = None, error: str | None = None) -> Job:
        return self.transition(job_id, status, result=result, error=error)

    def complete(self, job_id: str, status: JobStatus, result: RunResult | None = None,
                 error: str | None = None, render=None) -> Job:
        """Finish a job the worker ran, honouring a cancel request in the same transaction: a cancel that
        arrives before this commits turns a would-be success into `cancelled`, never `succeeded` with the
        flag still set. `render(job, result)` (the report) runs after that decision, so the persisted report
        describes the status the job actually ended in."""
        def mutate(job: Job) -> None:
            final, why = status, error
            cause = self.stop_cause(job.work_item.agent_id) if status == JobStatus.SUCCEEDED else None
            if job.cancel_requested and status == JobStatus.SUCCEEDED:
                final, why = JobStatus.CANCELLED, "cancelled during verification"
            elif cause:
                # Same rule for the emergency stops (the instance's and this job's agent's), read inside this write
                # transaction: a switch engaged at any point before the success commits wins, even if every
                # command happened to finish between polls.
                final, why = JobStatus.CANCELLED, cause
            if final not in TRANSITIONS[job.status]:
                raise InvalidTransition(f"{job.status.value} -> {final.value} is not allowed for {job_id}")
            job.status, job.error = final, why
            if result is not None and render is not None:
                result.report_markdown = render(job, result)
            job.result = result
        return self._update(job_id, mutate)

    @_serialised
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
