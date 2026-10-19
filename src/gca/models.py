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


class Approval(BaseModel):
    approver: str
    approved_at: str = Field(default_factory=now_iso)
    note: str = ""


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
