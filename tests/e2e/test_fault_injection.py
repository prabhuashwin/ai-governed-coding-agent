"""Fault injection with real OS processes: killed workers, concurrent workers, and an unreliable receiver.

These run `gca` as separate processes (python -m gca.cli) so crashes and races are real, not simulated.
"""

from __future__ import annotations

import json
import os
import random
import shutil
import signal
import sqlite3
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest
from conftest import CALC_FIX_NEW, CALC_FIX_OLD, CALC_SRC, CALC_TEST, git, make_repo

from gca.audit import AuditLog, verify_chain
from gca.config import Config, WebhookConfig
from gca.models import JobStatus
from gca.queue import JobQueue
from gca.webhooks import Deliverer, Outbox, verify_signature

pytestmark = [pytest.mark.e2e, pytest.mark.skipif(sys.platform == "win32", reason="uses POSIX signals")]

PY = sys.executable
TEST_CMD = f"{PY} -m pytest -q -p no:cacheprovider"


def write_config(tmp: Path, data_dir: Path, script: Path, *, worker_id: str = "worker-1", extra: str = "") -> Path:
    cfg = tmp / f"gca-{worker_id}.toml"
    cfg.write_text(
        f'data_dir = "{data_dir.as_posix()}"\nworker_id = "{worker_id}"\nstale_heartbeat_seconds = 2\n'
        f'[provider]\nkind = "scripted"\nscript_path = "{script.as_posix()}"\n'
        f'[policy]\nallowed_commands = ["python", "{Path(PY).name}"]\n' + extra)
    return cfg


def gca(cfg: Path, *args: str, **kw) -> subprocess.CompletedProcess:
    return subprocess.run([PY, "-m", "gca.cli", "-c", str(cfg), *args], capture_output=True, text=True,
                          timeout=kw.pop("timeout", 120), **kw)


def gca_bg(cfg: Path, *args: str) -> subprocess.Popen:
    """Start gca in the background; output goes to <config>.log (read it when a test fails)."""
    with open(cfg.with_suffix(".log"), "ab") as log:  # the child keeps its own copy of the descriptor
        return subprocess.Popen([PY, "-m", "gca.cli", "-c", str(cfg), *args], stdout=log, stderr=subprocess.STDOUT)


def fix_steps(sleep_first: float = 0.0) -> list[dict]:
    first = {"tool_calls": [{"name": "run_tests", "arguments": {}}]}
    if sleep_first:
        first["sleep"] = sleep_first
    return [first,
            {"tool_calls": [{"name": "edit_file", "arguments": {"path": "calc.py", "old": CALC_FIX_OLD, "new": CALC_FIX_NEW}}]},
            {"tool_calls": [{"name": "run_tests", "arguments": {}}]},
            {"tool_calls": [{"name": "finish", "arguments": {"summary": "fixed", "success": True}}]}]


def status_of(db: Path, job_id: str) -> str:
    conn = sqlite3.connect(str(db))
    try:
        row = conn.execute("SELECT status FROM jobs WHERE id=?", (job_id,)).fetchone()
        return row[0] if row else "?"
    finally:
        conn.close()


def wait_for(pred, timeout: float = 60, interval: float = 0.1) -> bool:
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if pred():
            return True
        time.sleep(interval)
    return False


def submit_and_approve(cfg: Path, repo: Path, title: str = "Fix mean()") -> str:
    r = gca(cfg, "submit", "--title", title, "--description", "off by one", "--repo", str(repo),
            "--test-cmd", TEST_CMD, "--requester", "alice")
    assert r.returncode == 0, r.stderr
    job_id = r.stdout.strip()
    assert gca(cfg, "approve", job_id, "--approver", "bob").returncode == 0
    return job_id


# ------------------------------------------------------------------ 1. worker killed mid-run
def test_sigkilled_worker_is_recovered_and_cleaned_up(tmp_path, calc_repo):
    data = tmp_path / "data"
    slow = tmp_path / "slow.json"
    slow.write_text(json.dumps({"steps": fix_steps(sleep_first=60)}))
    cfg = write_config(tmp_path, data, slow)
    job_id = submit_and_approve(cfg, calc_repo)

    worker = gca_bg(cfg, "run", "--once")
    db = data / "jobs.sqlite3"
    assert wait_for(lambda: status_of(db, job_id) == "running", 30), "worker never claimed the job"
    events = AuditLog(data / "audit", job_id)
    assert wait_for(lambda: any(e.event == "agent" or e.event == "started" for e in events.events()), 30)
    ws_path = next(e.data["path"] for e in events.events() if e.event == "workspace_created")
    assert Path(ws_path).exists()

    os.kill(worker.pid, signal.SIGKILL)  # no cleanup, no finally blocks
    worker.wait(timeout=10)
    assert status_of(db, job_id) == "running"  # the queue still thinks it is running
    time.sleep(2.5)  # > stale_heartbeat_seconds

    fast = tmp_path / "fast.json"
    fast.write_text(json.dumps({"steps": fix_steps()}))
    cfg2 = write_config(tmp_path, data, fast, worker_id="worker-2")
    r = gca(cfg2, "run", "--once")
    assert f"recovered stale job {job_id}" in r.stderr
    job = JobQueue(db).get(job_id)
    assert job.status == JobStatus.FAILED and job.error == "worker_crashed_or_stale_heartbeat"
    assert not Path(ws_path).exists(), "orphaned workspace was not removed"
    assert ws_path not in git(calc_repo, "worktree", "list")
    tail = [e.event for e in AuditLog(data / "audit", job_id).events()][-2:]
    assert tail == ["recovered", "finished"]
    assert verify_chain(data / "audit" / f"{job_id}.jsonl")[0]  # partial log from the dead worker still verifies

    # the system is healthy afterwards: a new job on the same repo succeeds
    new_id = submit_and_approve(cfg2, calc_repo, "retry")
    r = gca(cfg2, "run", "--once")
    assert r.returncode == 0 and "succeeded" in r.stdout, r.stdout + r.stderr
    assert status_of(db, new_id) == "succeeded"


# ------------------------------------------------------------------ 2. concurrent workers
@pytest.mark.parametrize("same_repo", [False, True], ids=["separate-repos", "same-repo"])
def test_concurrent_workers_never_double_claim(tmp_path, same_repo):
    data = tmp_path / "data"
    script = tmp_path / "s.json"
    script.write_text(json.dumps({"steps": fix_steps()}))
    n_jobs, n_workers = 8, 4
    repos = [make_repo(tmp_path / f"repo{i}", {"calc.py": CALC_SRC, "tests/test_calc.py": CALC_TEST,
                                                "tests/__init__.py": "",
                                                "conftest.py": "import sys, pathlib\nsys.path.insert(0, str(pathlib.Path(__file__).parent))\n"})
             for i in range(1 if same_repo else n_jobs)]
    cfgs = [write_config(tmp_path, data, script, worker_id=f"w{i}") for i in range(n_workers)]
    jobs = [submit_and_approve(cfgs[0], repos[0 if same_repo else i], f"job {i}") for i in range(n_jobs)]

    procs = [gca_bg(c, "run", "--poll", "0.2") for c in cfgs]
    db = data / "jobs.sqlite3"
    try:
        done = wait_for(lambda: all(status_of(db, j) in ("succeeded", "failed") for j in jobs), 180, 0.5)
    finally:
        for p in procs:
            p.terminate()
        for p in procs:
            try:
                p.wait(timeout=10)
            except subprocess.TimeoutExpired:
                p.kill()
    assert done, {j: status_of(db, j) for j in jobs}
    q = JobQueue(db)
    claimers = set()
    for j in jobs:
        job = q.get(j)
        assert job.status == JobStatus.SUCCEEDED, (j, job.error)
        assert job.attempts == 1
        claims = [e for e in AuditLog(data / "audit", j).events() if e.event == "claimed"]
        assert len(claims) == 1, f"{j} claimed {len(claims)} times"
        claimers.add(claims[0].actor)
    assert len(claimers) >= 2, f"work was not shared across workers: {claimers}"
    for r in repos:
        assert len(git(r, "worktree", "list").strip().splitlines()) == 1  # no leftover worktrees


# ------------------------------------------------------------------ 3. unreliable receiver
class _Flaky(BaseHTTPRequestHandler):
    seen: list[str] = []
    fail_rate = 0.5
    secret = "s"

    def do_POST(self):  # noqa: N802
        body = self.rfile.read(int(self.headers["Content-Length"]))
        if verify_signature(self.secret, "POST", self.path, dict(self.headers), body) is not None:
            self.send_response(401)
            self.end_headers()
            return
        if random.random() < self.fail_rate:
            self.send_response(random.choice([500, 502, 503, 429]))
            self.end_headers()
            return
        _Flaky.seen.append(json.loads(body)["id"])
        self.send_response(204)
        self.end_headers()

    def log_message(self, *a):
        pass


def test_events_survive_a_flaky_and_restarting_receiver(tmp_path, monkeypatch):
    monkeypatch.setenv("GCA_WEBHOOK_SECRET", "s")
    random.seed(7)
    _Flaky.seen = []
    srv = ThreadingHTTPServer(("127.0.0.1", 0), _Flaky)
    port = srv.server_address[1]
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    url = f"http://127.0.0.1:{port}/hook"
    cfg = Config(data_dir=str(tmp_path / "d"), webhooks=WebhookConfig(enabled=True, outbound_urls=[url], max_attempts=50))
    ob = Outbox(cfg.db_path)
    d = Deliverer(cfg, ob)
    ids = [f"evt_{i}" for i in range(30)]
    for i in ids:
        ob.add({"id": i, "type": "job.finished", "created_at": "2026-01-01T00:00:00+00:00", "job_id": "j", "data": {}}, [url])

    def drain(rounds: int):
        for _ in range(rounds):
            d.deliver_pending()
            ob._conn.execute("UPDATE webhook_outbox SET next_attempt_at='2000-01-01' WHERE status='pending'")

    drain(3)
    # receiver goes down completely (connection refused), then comes back on the same port
    srv.shutdown()
    srv.server_close()
    stats = d.deliver_pending()
    pending_before = len(ob.rows("pending"))
    assert stats["delivered"] == 0 and pending_before > 0
    srv2 = ThreadingHTTPServer(("127.0.0.1", port), _Flaky)
    threading.Thread(target=srv2.serve_forever, daemon=True).start()
    try:
        drain(40)
    finally:
        srv2.shutdown()
        srv2.server_close()
    assert ob.rows("pending") == [] and ob.rows("dead") == []
    assert set(_Flaky.seen) == set(ids), "an event was lost"
    assert len(_Flaky.seen) == len(ids), "an event was delivered twice after a 2xx"


def test_cleanup_helper_ignores_non_repos(tmp_path):
    from gca.workspace import cleanup_orphan_worktree
    assert cleanup_orphan_worktree(tmp_path / "nope", tmp_path / "wt") is False
    assert shutil.which("git")
