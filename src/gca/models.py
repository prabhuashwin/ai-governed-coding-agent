"""Core data models. Everything that crosses a component boundary is a pydantic model."""

from __future__ import annotations

import sqlite3
import uuid
import weakref
from datetime import datetime, timezone
from enum import Enum
from typing import Any

from pydantic import BaseModel, Field


def open_db(owner: object, path: str) -> sqlite3.Connection:
    """Open an SQLite connection that is closed when `owner` is garbage-collected (or at interpreter exit).

    Deterministic closing matters on Windows, where an open handle locks the database file.
    """
    conn = sqlite3.connect(path, isolation_level=None, check_same_thread=False)
    conn.execute("PRAGMA busy_timeout=5000")
    weakref.finalize(owner, conn.close)
    return conn


def _retry_locked(fn, *, seconds: float = 10.0):
    """Call `fn` until it stops raising 'database is locked'. The busy handler covers ordinary statements, but a
    few schema-level operations (changing the journal mode, DDL during another connection's write) can return
    SQLITE_BUSY immediately on some platforms, so startup under contention retries them explicitly."""
    import time

    deadline = time.monotonic() + seconds
    while True:
        try:
            return fn()
        except sqlite3.OperationalError as e:
            if "locked" not in str(e).lower() and "busy" not in str(e).lower() or time.monotonic() >= deadline:
                raise
            time.sleep(0.02)


def set_wal(conn: sqlite3.Connection) -> None:
    """Switch to WAL journaling; harmless to skip if another connection is mid-write at the moment (WAL is a
    performance setting, and the next opener will set it)."""
    try:
        _retry_locked(lambda: conn.execute("PRAGMA journal_mode=WAL"), seconds=2.0)
    except sqlite3.OperationalError:
        pass


def add_column_if_missing(conn: sqlite3.Connection, table: str, column: str, decl: str) -> None:
    """Schema migration safe under concurrent startup: the check and the ALTER run in one write transaction, and
    a second process that lost the race sees the column already there (or a benign duplicate-column error)."""
    def once() -> None:
        cur = conn.cursor()
        cur.execute("BEGIN IMMEDIATE")
        try:
            if column not in {r[1] for r in cur.execute(f"PRAGMA table_info({table})")}:
                cur.execute(f"ALTER TABLE {table} ADD COLUMN {column} {decl}")
            cur.execute("COMMIT")
        except sqlite3.OperationalError as e:
            cur.execute("ROLLBACK")
            if "duplicate column" not in str(e).lower():
                raise

    _retry_locked(once)


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def new_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:12]}"


class JobStatus(str, Enum):
    SUBMITTED = "submitted"
    APPROVED = "approved"
    REJECTED = "rejected"
    RUNNING = "running"
    VERIFYING = "verifying"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELLED = "cancelled"


TERMINAL_STATES = {JobStatus.REJECTED, JobStatus.SUCCEEDED, JobStatus.FAILED, JobStatus.CANCELLED}

# Allowed state transitions. The queue refuses anything else.
TRANSITIONS: dict[JobStatus, set[JobStatus]] = {
    JobStatus.SUBMITTED: {JobStatus.APPROVED, JobStatus.REJECTED, JobStatus.CANCELLED},
    JobStatus.APPROVED: {JobStatus.RUNNING, JobStatus.CANCELLED},
    JobStatus.RUNNING: {JobStatus.VERIFYING, JobStatus.FAILED, JobStatus.CANCELLED},
    JobStatus.VERIFYING: {JobStatus.SUCCEEDED, JobStatus.FAILED, JobStatus.CANCELLED},
    JobStatus.REJECTED: set(),
    JobStatus.SUCCEEDED: set(),
    JobStatus.FAILED: set(),
    JobStatus.CANCELLED: set(),
}


class Budget(BaseModel):
    """Hard limits enforced by the harness. The model cannot change these."""

    max_turns: int = 30
    max_input_tokens: int = 400_000
    max_output_tokens: int = 60_000
    max_cost_usd: float = 2.0
    max_seconds: int = 900
    max_tool_calls: int = 120
    # Diff-size limits checked by the verifier: a small bug fix should not touch half the repo.
    max_files_changed: int = 20
    max_lines_changed: int = 800  # insertions + deletions


class WorkItem(BaseModel):
    """What an engineer submits. Text fields are UNTRUSTED input to the agent."""

    id: str = Field(default_factory=lambda: new_id("wi"))
    title: str
    description: str
    acceptance_criteria: list[str]
    repo_path: str
    base_ref: str = "HEAD"
    test_command: list[str] = Field(default_factory=lambda: ["python", "-m", "pytest", "-q"])
    allowed_paths: list[str] = Field(default_factory=list)  # empty = whole repo
    requester: str = "unknown"
    source: str = "local"  # local | servicenow | ...
    source_ref: str | None = None  # e.g. ServiceNow sys_id
    idempotency_key: str | None = None


class Restrictions(BaseModel):
    """Enforceable approval conditions. Each one can only *narrow* what the work item and operator policy already
    allow: paths must lie inside the work item's allowed paths (if any), commands inside the operator's allow-list,
    and budget fields can only go down."""
    allowed_paths: list[str] | None = None
    allowed_commands: list[str] | None = None
    budget: dict[str, float | int] | None = None

    _BUDGET_FIELDS = ("max_turns", "max_input_tokens", "max_output_tokens", "max_cost_usd", "max_seconds",
                      "max_tool_calls", "max_files_changed", "max_lines_changed")

    def apply(self, job: Job, operator_commands: list[str]) -> list[str]:
        """Narrow `job` in place. Returns human-readable lines describing what was applied. Raises ValueError when a
        restriction would widen scope or leave nothing allowed."""
        applied: list[str] = []
        if self.allowed_paths is not None:
            wanted = [p.strip().strip("/") for p in self.allowed_paths if p.strip().strip("/")]
            if not wanted:
                raise ValueError("allowed_paths must name at least one path")
            existing = [p.strip("/") for p in job.work_item.allowed_paths]
            if existing:
                inside = [p for p in wanted if any(p == e or p.startswith(e + "/") for e in existing)]
                if len(inside) != len(wanted):
                    outside = sorted(set(wanted) - set(inside))
                    raise ValueError(f"allowed_paths {outside} lie outside the work item's allowed paths {existing}")
            job.work_item.allowed_paths = wanted
            applied.append(f"paths limited to {wanted}")
        if self.allowed_commands is not None:
            wanted_cmds = [c.strip() for c in self.allowed_commands if c.strip()]
            if not wanted_cmds:
                raise ValueError("allowed_commands must name at least one command")
            unknown = sorted(set(wanted_cmds) - set(operator_commands))
            if unknown:
                raise ValueError(f"allowed_commands {unknown} are not in the operator's command allow-list")
            applied.append(f"commands limited to {wanted_cmds}")
        if self.budget:
            for k, v in self.budget.items():
                if k not in self._BUDGET_FIELDS:
                    raise ValueError(f"unknown budget field {k!r}")
                current = getattr(job.budget, k)
                if v > current:
                    raise ValueError(f"budget.{k}={v} would raise the job's limit of {current}")
                setattr(job.budget, k, type(current)(v))
                applied.append(f"budget.{k} lowered to {v}")
        return applied


class Approval(BaseModel):
    approver: str
    approved_at: str = Field(default_factory=now_iso)
    note: str = ""
    conditions: list[str] = Field(default_factory=list)  # free text, for humans
    restrictions: Restrictions | None = None  # enforced


class Usage(BaseModel):
    input_tokens: int = 0
    output_tokens: int = 0
    cost_usd: float = 0.0
    turns: int = 0
    tool_calls: int = 0
    seconds: float = 0.0

    def add(self, other: Usage) -> None:
        self.input_tokens += other.input_tokens
        self.output_tokens += other.output_tokens
        self.cost_usd += other.cost_usd
        self.turns += other.turns
        self.tool_calls += other.tool_calls
        self.seconds += other.seconds


class StopReason(str, Enum):
    FINISHED = "finished"  # agent called finish()
    BUDGET_EXHAUSTED = "budget_exhausted"
    CANCELLED = "cancelled"
    PROVIDER_ERROR = "provider_error"
    POLICY_HALT = "policy_halt"  # too many blocked actions
    KILL_SWITCH = "kill_switch"  # global stop engaged (locally or via inbound webhook)
    INTERNAL_ERROR = "internal_error"


class AgentOutcome(BaseModel):
    stop_reason: StopReason
    summary: str = ""
    claimed_success: bool = False
    usage: Usage = Field(default_factory=Usage)
    blocked_actions: int = 0
    detail: str = ""
    explicit_finish: bool = False  # the model called finish(); False when the loop ended for any other reason


class VerificationResult(BaseModel):
    passed: bool
    tests_ran: bool
    exit_code: int | None = None
    output_tail: str = ""
    flags: list[str] = Field(default_factory=list)  # e.g. "test_file_deleted:tests/test_x.py"
    files_changed: list[str] = Field(default_factory=list)
    insertions: int = 0
    deletions: int = 0
    notes: list[str] = Field(default_factory=list)  # reviewer warnings that do not fail the job
    commit: str | None = None  # the exact commit that was verified; publishing pushes this and nothing else


class RunResult(BaseModel):
    job_id: str
    branch: str | None = None
    diff: str = ""
    agent: AgentOutcome | None = None
    verification: VerificationResult | None = None
    report_markdown: str = ""
    provider: str = ""
    model: str = ""


class Job(BaseModel):
    id: str = Field(default_factory=lambda: new_id("job"))
    work_item: WorkItem
    status: JobStatus = JobStatus.SUBMITTED
    budget: Budget = Field(default_factory=Budget)
    approval: Approval | None = None
    created_at: str = Field(default_factory=now_iso)
    updated_at: str = Field(default_factory=now_iso)
    worker_id: str | None = None
    heartbeat_at: str | None = None
    attempts: int = 0
    result: RunResult | None = None
    error: str | None = None
    cancel_requested: bool = False


class AuditEvent(BaseModel):
    seq: int
    ts: str
    job_id: str
    actor: str  # requester | approver | worker:<id> | agent | verifier | policy
    event: str
    data: dict[str, Any] = Field(default_factory=dict)
    prev_hash: str
    hash: str


Restrictions.model_rebuild()
