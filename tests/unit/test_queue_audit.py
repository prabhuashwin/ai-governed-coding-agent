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


# --------------------------------------------------------------- review findings (2026-10-19)
def test_audit_writers_in_separate_processes_keep_one_chain(tmp_path: Path):
    """Two AuditLog instances for one job (a worker and a `gca cancel` in another process) each knew the tail
    only from construction time, so an interleaved append produced seq [1, 2, 2] and a broken chain."""
    worker = AuditLog(tmp_path, "job_a")
    canceller = AuditLog(tmp_path, "job_a")  # constructed while the log is empty, like a second process
    worker.record("worker", "started")
    canceller.record("alice", "cancel_requested")
    worker.record("worker", "finished")
    assert [e.seq for e in worker.events()] == [1, 2, 3]
    ok, msg = verify_chain(worker.path)
    assert ok, msg


def test_audit_appends_from_many_processes_verify(tmp_path: Path):
    import subprocess
    import sys

    code = ("import sys; from gca.audit import AuditLog; "
            "log = AuditLog(sys.argv[1], 'job_p'); [log.record(sys.argv[2], 'tick', i=i) for i in range(25)]")
    procs = [subprocess.Popen([sys.executable, "-c", code, str(tmp_path), f"p{i}"]) for i in range(4)]
    assert all(p.wait() == 0 for p in procs)
    log = AuditLog(tmp_path, "job_p")
    assert len(log.events()) == 100
    ok, msg = verify_chain(log.path)
    assert ok, msg


def test_stale_cancel_never_reverts_a_finished_job(q: JobQueue):
    """request_cancel used to read the whole job, set a flag and write the whole job back, so a cancel that
    raced the worker's finish put a succeeded job back to `verifying`."""
    job = q.submit(wi())
    q.approve(job.id, "a")
    q.claim_next("w")
    q.transition(job.id, JobStatus.VERIFYING)
    q.finish(job.id, JobStatus.SUCCEEDED)
    after = q.request_cancel(job.id)  # arrives after the finish
    assert after.status == JobStatus.SUCCEEDED and not after.cancel_requested
    assert q.get(job.id).status == JobStatus.SUCCEEDED


def test_concurrent_cancel_and_finish_serialise(q: JobQueue):
    """Many cancel requests racing a finish: the final row is either cancelled-flagged-then-finished or
    finished, never a stale copy of an earlier state."""
    job = q.submit(wi())
    q.approve(job.id, "a")
    q.claim_next("w")
    q.transition(job.id, JobStatus.VERIFYING)
    go = threading.Barrier(9)

    def cancel():
        go.wait()
        q.request_cancel(job.id)

    def finish():
        go.wait()
        q.finish(job.id, JobStatus.SUCCEEDED)

    ts = [threading.Thread(target=cancel) for _ in range(8)] + [threading.Thread(target=finish)]
    for t in ts:
        t.start()
    for t in ts:
        t.join()
    assert q.get(job.id).status == JobStatus.SUCCEEDED


# --------------------------------------------------------------- review round 2 (2026-10-19)
def test_rollback_in_one_thread_cannot_undo_another_threads_commit(q: JobQueue):
    """Threads share one connection. An invalid transition's ROLLBACK used to also undo a submit or a kill
    switch another thread had just been told succeeded."""
    import threading

    done = q.submit(wi())
    q.approve(done.id, "a")
    q.claim_next("w")
    q.transition(done.id, JobStatus.VERIFYING)
    q.finish(done.id, JobStatus.SUCCEEDED)  # terminal: approving it again is invalid
    go = threading.Barrier(3)
    submitted: list[str] = []

    def bad_approve():
        go.wait()
        for _ in range(50):
            with pytest.raises(InvalidTransition):
                q.approve(done.id, "x")

    def submits():
        go.wait()
        for _ in range(50):
            submitted.append(q.submit(wi()).id)

    def kill():
        go.wait()
        for _ in range(50):
            q.set_kill_switch(True, reason="drill", actor="t")

    ts = [threading.Thread(target=f) for f in (bad_approve, submits, kill)]
    for t in ts:
        t.start()
    for t in ts:
        t.join()
    ids = {j.id for j in q.list()}
    assert all(s in ids for s in submitted), "a returned submission was rolled back"
    assert q.kill_switch() is not None, "an acknowledged kill switch was rolled back"


def test_complete_turns_a_late_cancel_into_cancelled(q: JobQueue):
    job = q.submit(wi())
    q.approve(job.id, "a")
    q.claim_next("w")
    q.transition(job.id, JobStatus.VERIFYING)
    q.request_cancel(job.id)  # lands after the worker's own cancel check, before it commits success
    final = q.complete(job.id, JobStatus.SUCCEEDED)
    assert final.status == JobStatus.CANCELLED and final.error == "cancelled during verification"
    other = q.submit(wi())
    q.approve(other.id, "a")
    q.claim_next("w")
    q.transition(other.id, JobStatus.VERIFYING)
    assert q.complete(other.id, JobStatus.SUCCEEDED).status == JobStatus.SUCCEEDED
