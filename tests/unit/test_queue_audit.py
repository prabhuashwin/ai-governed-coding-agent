"""Job queue state machine + hash-chained audit log."""

from __future__ import annotations

import json
import threading
from pathlib import Path

import pytest

from gca.audit import AuditLog, verify_chain
from gca.models import Budget, JobStatus, WorkItem
from gca.queue import InvalidTransition, JobQueue


def wi(**kw) -> WorkItem:
    base = dict(title="t", description="d", acceptance_criteria=["c"], repo_path="/r")
    base.update(kw)
    return WorkItem(**base)


@pytest.fixture
def q(tmp_path: Path) -> JobQueue:
    return JobQueue(tmp_path / "jobs.sqlite3")


def test_submit_is_idempotent(q: JobQueue):
    a = q.submit(wi(idempotency_key="k1"))
    b = q.submit(wi(idempotency_key="k1"))
    c = q.submit(wi(idempotency_key="k2"))
    assert a.id == b.id and a.id != c.id
    assert len(q.list()) == 2


def test_happy_path_transitions(q: JobQueue):
    job = q.submit(wi())
    assert job.status == JobStatus.SUBMITTED
    assert q.approve(job.id, "alice").status == JobStatus.APPROVED
    claimed = q.claim_next("w1")
    assert claimed and claimed.id == job.id and claimed.status == JobStatus.RUNNING and claimed.worker_id == "w1"
    assert q.transition(job.id, JobStatus.VERIFYING).status == JobStatus.VERIFYING
    assert q.finish(job.id, JobStatus.SUCCEEDED).status == JobStatus.SUCCEEDED


@pytest.mark.parametrize("to", [JobStatus.RUNNING, JobStatus.SUCCEEDED, JobStatus.VERIFYING, JobStatus.FAILED])
def test_illegal_transitions_from_submitted(q: JobQueue, to: JobStatus):
    job = q.submit(wi())
    with pytest.raises(InvalidTransition):
        q.transition(job.id, to)


def test_terminal_states_are_final(q: JobQueue):
    job = q.submit(wi())
    q.reject(job.id, "bob")
    with pytest.raises(InvalidTransition):
        q.approve(job.id, "alice")


def test_unapproved_jobs_are_never_claimed(q: JobQueue):
    q.submit(wi())
    assert q.claim_next("w1") is None


def test_claim_is_atomic_across_threads(q: JobQueue, tmp_path: Path):
    for i in range(5):
        q.approve(q.submit(wi(title=f"j{i}")).id, "a")
    claimed: list[str] = []
    lock = threading.Lock()

    def worker(name: str):
        local = JobQueue(tmp_path / "jobs.sqlite3")
        while True:
            j = local.claim_next(name)
            if j is None:
                break
            with lock:
                claimed.append(j.id)

    ts = [threading.Thread(target=worker, args=(f"w{i}",)) for i in range(4)]
    [t.start() for t in ts]
    [t.join() for t in ts]
    assert len(claimed) == 5 and len(set(claimed)) == 5


def test_cancel_before_run_is_immediate_and_during_run_is_flagged(q: JobQueue):
    a = q.submit(wi())
    assert q.request_cancel(a.id).status == JobStatus.CANCELLED
    b = q.submit(wi())
    q.approve(b.id, "a")
    q.claim_next("w")
    j = q.request_cancel(b.id)
    assert j.status == JobStatus.RUNNING and j.cancel_requested
    assert q.cancel_requested(b.id)


def test_stale_running_jobs_are_recovered(q: JobQueue):
    job = q.submit(wi())
    q.approve(job.id, "a")
    q.claim_next("dead-worker")
    assert q.recover_stale(stale_after_seconds=3600) == []  # fresh heartbeat
    recovered = q.recover_stale(stale_after_seconds=-1)  # everything is stale
    assert [j.id for j in recovered] == [job.id]
    assert q.get(job.id).status == JobStatus.FAILED
    assert "stale" in q.get(job.id).error


def test_budget_persisted_with_job(q: JobQueue):
    job = q.submit(wi(), Budget(max_cost_usd=0.25))
    assert q.get(job.id).budget.max_cost_usd == 0.25


# -------------------------------------------------------------------- audit
def test_audit_chain_verifies_and_detects_tampering(tmp_path: Path):
    log = AuditLog(tmp_path, "job_x")
    log.record("tester", "submitted", a=1)
    log.record("worker:1", "claimed")
    log.record("agent", "tool_call", tool="read_file", args={"path": "x"})
    ok, msg = verify_chain(log.path)
    assert ok, msg

    # reopening continues the chain
    log2 = AuditLog(tmp_path, "job_x")
    log2.record("verifier", "verified", passed=True)
    assert verify_chain(log.path)[0]
    assert [e.seq for e in log2.events()] == [1, 2, 3, 4]

    # tamper with the middle event
    lines = log.path.read_text().splitlines()
    ev = json.loads(lines[1])
    ev["actor"] = "attacker"
    lines[1] = json.dumps(ev)
    log.path.write_text("\n".join(lines) + "\n")
    ok, msg = verify_chain(log.path)
    assert not ok and "hash mismatch" in msg

    # delete an event
    log.path.write_text("\n".join([lines[0], lines[2], lines[3]]) + "\n")
    ok, msg = verify_chain(log.path)
    assert not ok
