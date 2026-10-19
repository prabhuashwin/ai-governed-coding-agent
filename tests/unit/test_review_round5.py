"""Regression tests for the fifth external review (19 October 2026, main @ e05ad46): eight findings."""

from __future__ import annotations

import json
import sqlite3
import sys
import threading
import time
from contextlib import closing
from datetime import datetime, timedelta, timezone
from pathlib import Path

import httpx
import pytest
from conftest import CALC_FIX_NEW, CALC_FIX_OLD, git, steps_fix_calc

from gca.config import ApprovalConfig, WebhookConfig
from gca.inbound import CommandHandler, make_server, set_kill_switch
from gca.models import Budget, JobStatus, Restrictions
from gca.providers.testing import ScriptedProvider
from gca.publish import api_host, github_slug, publish_job
from gca.queue import JobQueue
from gca.retention import prune
from gca.webhooks import signed_headers
from gca.worker import Worker
from gca.workspace import run_until_stopped


# ---------------------------------------------------------------- 1 (P1) retention vs released reservations
def test_prune_keeps_a_released_reservations_history(config, monkeypatch):
    """A failed delivery's row carries a 1970 *lease* so a retry can take it over at once; its first-received
    time is recent. Pruning must age rows by that immutable time, or a fresh delivery's history disappears and
    a stale kill-switch "off" replayed later could undo a newer stop."""
    monkeypatch.setenv("GCA_INBOUND_SECRET", "in")
    config.webhooks = WebhookConfig(enabled=True)
    q = JobQueue(config.db_path)
    h = CommandHandler(config, q)
    now = datetime.now(timezone.utc).isoformat()
    h._conn.execute("INSERT INTO inbound_deliveries (id, received_at, status, response, first_received_at) VALUES "
                    "('fresh-failed', '1970-01-01T00:00:00+00:00', 0, '', ?)", (now,))
    old = (datetime.now(timezone.utc) - timedelta(days=40)).isoformat()
    h._conn.execute("INSERT INTO inbound_deliveries (id, received_at, status, response, first_received_at) VALUES "
                    "('genuinely-old', ?, 200, '{}', ?)", (old, old))
    q.close()
    h._conn.close()
    assert prune(config) == {"jobs": 0, "webhook_outbox": 0, "inbound_deliveries": 1}
    with closing(sqlite3.connect(config.db_path)) as c:
        assert [r[0] for r in c.execute("SELECT id FROM inbound_deliveries")] == ["fresh-failed"]


# ---------------------------------------------------------------- 2 (P1) process tree + bounded draining
@pytest.mark.skipif(sys.platform == "win32", reason="process-group semantics differ; covered by taskkill /T")
def test_stop_kills_the_whole_process_tree_and_bounds_draining(tmp_path):
    """The parent spawns a grandchild that keeps writing a file and holds the output pipes. A stop must end the
    grandchild too (no writes after the stop) and must not hang on draining the pipes it holds."""
    marker = tmp_path / "writes.txt"
    child = tmp_path / "child.py"
    child.write_text(f"import time\nwhile True:\n    open({str(marker)!r}, 'a').write('x')\n    time.sleep(0.05)\n")
    parent = [sys.executable, "-c", f"import subprocess, sys, time\nsubprocess.Popen([sys.executable, {str(child)!r}])\n"
                                    "time.sleep(30)"]
    stop_at = time.monotonic() + 1.0
    t0 = time.monotonic()
    res = run_until_stopped(parent, cwd=str(tmp_path), env=None, timeout=60, stop_check=lambda: time.monotonic() > stop_at)
    assert res.interrupted and time.monotonic() - t0 < 10
    size = marker.stat().st_size
    time.sleep(0.5)
    assert marker.stat().st_size == size, "the grandchild kept writing after the stop"


# ---------------------------------------------------------------- 3 (P2) a stop between polls still counts
def test_stop_during_a_fast_command_is_not_missed():
    """A command that finishes within one poll interval never reaches the in-loop check; the stop that landed
    while it ran must still void its result."""
    flag = {"stop": False}

    def stop_check():
        r = flag["stop"]
        flag["stop"] = True  # first call (before start) says no; the command runs; the check after it says yes
        return r

    res = run_until_stopped([sys.executable, "-c", "print('done')"], cwd=None, env=None, timeout=10, stop_check=stop_check)
    assert res.interrupted and "done" in res.stdout
    res = run_until_stopped([sys.executable, "-c", "print(1)"], cwd=None, env=None, timeout=10, stop_check=lambda: True)
    assert res.interrupted and res.exit_code == -1  # not even started


def test_kill_switch_engaged_during_a_fast_verification_never_yields_success(config, work_item):
    """Engage the switch while the (very quick) verifier run is in flight: no poll may observe it, but the
    final transaction must. The job ends cancelled/kill_switch, not succeeded."""
    q = JobQueue(config.db_path)
    job = q.submit(work_item)
    q.approve(job.id, "lead")
    steps = [{"tool_calls": [{"name": "edit_file", "arguments": {"path": "calc.py", "old": CALC_FIX_OLD, "new": CALC_FIX_NEW}}]},
             {"tool_calls": [{"name": "finish", "arguments": {"summary": "done", "success": True}}]}]
    original = JobQueue.complete

    def engage_then_complete(self, job_id, status, *a, **kw):
        if status == JobStatus.SUCCEEDED:
            JobQueue(config.db_path).set_kill_switch(True, reason="drill", actor="secops")
        return original(self, job_id, status, *a, **kw)

    JobQueue.complete = engage_then_complete  # type: ignore[method-assign]
    try:
        job = Worker(config, q, provider_factory=lambda: ScriptedProvider(steps)).run_once()
    finally:
        JobQueue.complete = original  # type: ignore[method-assign]
    assert job.status == JobStatus.CANCELLED and job.error == "kill_switch"
    assert "status **cancelled**" in job.result.report_markdown


# ---------------------------------------------------------------- 4 (P2) superseded deliveries replay cleanly
def test_superseded_kill_switch_delivery_replays_its_409(config, monkeypatch):
    monkeypatch.setenv("GCA_INBOUND_SECRET", "in")
    config.webhooks = WebhookConfig(enabled=True)
    q = JobQueue(config.db_path)
    h = CommandHandler(config, q)
    set_kill_switch(config, q, True, reason="incident", actor="secops", via="webhook", delivery_id="on-new")
    body = {"enabled": False, "actor": "secops", "reason": "resolved"}
    raw = json.dumps(body).encode()
    stale = "2026-01-01T00:00:00+00:00"
    h._conn.execute("INSERT INTO inbound_deliveries (id, received_at, status, response, first_received_at) "
                    "VALUES ('off-old', ?, 0, '', ?)", (stale, stale))
    hd = signed_headers("in", "POST", "/v1/controls/kill-switch", raw, delivery_id="off-old")
    first = h.handle("POST", "/v1/controls/kill-switch", hd, raw)
    second = h.handle("POST", "/v1/controls/kill-switch", hd, raw)  # replay: served from the row, must not raise
    assert first[0] == 409 and "superseded" in first[1]["error"]
    assert second[0] == 409 and second[1]["replayed"] is True and "superseded" in second[1]["error"]
    assert q.kill_switch() is not None  # the newer "on" stands


# ---------------------------------------------------------------- 5 (P2) non-finite budgets
@pytest.mark.parametrize("bad", ["NaN", "inf", "-inf", float("nan"), float("inf")])
def test_non_finite_budget_values_are_rejected_everywhere(config, work_item, bad, monkeypatch):
    with pytest.raises(ValueError):
        Budget.model_validate({"max_cost_usd": bad})
    with pytest.raises(ValueError):
        Restrictions.model_validate({"budget": {"max_cost_usd": bad}})
    # and through the inbound boundary: the job stays readable and approvable afterwards
    monkeypatch.setenv("GCA_INBOUND_SECRET", "in")
    config.webhooks = WebhookConfig(enabled=True)
    config.approval = ApprovalConfig(mode="webhook")
    q = JobQueue(config.db_path)
    h = CommandHandler(config, q)
    job = q.submit(work_item)
    path = f"/v1/jobs/{job.id}/decision"
    raw = json.dumps({"decision": "approve", "approver": "lead", "restrictions": {"budget": {"max_cost_usd": bad}}},
                     default=str).encode()
    s, r = h.handle("POST", path, signed_headers("in", "POST", path, raw, delivery_id=f"d-{bad}"), raw)
    assert s == 422
    assert q.get(job.id).status == JobStatus.SUBMITTED and q.list()  # still readable, still pending
    raw = json.dumps({"title": "t", "description": "d", "acceptance_criteria": [], "repo_path": work_item.repo_path,
                      "test_command": ["pytest"], "requester": "dev", "budget": {"max_cost_usd": bad}}, default=str).encode()
    s, r = h.handle("POST", "/v1/work-items", signed_headers("in", "POST", "/v1/work-items", raw, delivery_id=f"w-{bad}"), raw)
    assert s == 422 and len(q.list()) == 1


# ---------------------------------------------------------------- 6 (P2) narrowed commands never block run_tests
def test_narrowed_commands_do_not_block_the_approved_test_command(config, work_item):
    """The approver allows only `pytest`; the work item's test command starts with the interpreter. run_tests
    must still run (it is what the human approved and what the verifier runs); an ad-hoc command is blocked."""
    assert work_item.test_command[0] not in ("pytest",)
    q = JobQueue(config.db_path)
    job = q.submit(work_item)
    q.approve(job.id, "lead", restrictions=Restrictions(allowed_commands=["pytest"]),
              operator_commands=config.policy.allowed_commands)
    steps = [{"tool_calls": [{"name": "run_tests", "arguments": {}}]},
             {"tool_calls": [{"name": "edit_file", "arguments": {"path": "calc.py", "old": CALC_FIX_OLD, "new": CALC_FIX_NEW}}]},
             {"tool_calls": [{"name": "run_tests", "arguments": {}}]},
             {"tool_calls": [{"name": "finish", "arguments": {"summary": "done", "success": True}}]}]
    job = Worker(config, q, provider_factory=lambda: ScriptedProvider(steps)).run_once()
    assert job.status == JobStatus.SUCCEEDED and job.result.agent.blocked_actions == 0
    # the same restriction still blocks the interpreter as an ad-hoc command
    job2 = q.submit(work_item.model_copy(update={"idempotency_key": "k2"}))
    q.approve(job2.id, "lead", restrictions=Restrictions(allowed_commands=["pytest"]),
              operator_commands=config.policy.allowed_commands)
    steps2 = [{"tool_calls": [{"name": "run_command", "arguments": {"argv": [sys.executable, "-c", "print(1)"]}}]},
              {"tool_calls": [{"name": "finish", "arguments": {"summary": "done", "success": True}}]}]
    job2 = Worker(config, q, provider_factory=lambda: ScriptedProvider(steps2)).run_once()
    assert job2.result.agent.blocked_actions == 1


# ---------------------------------------------------------------- 7 (P2) GitHub failures become 502, never a dropped socket
@pytest.fixture
def pushed_job(config, work_item, calc_repo, tmp_path):
    bare = tmp_path / "remote.git"
    git(tmp_path, "init", "-q", "--bare", str(bare))
    git(calc_repo, "remote", "add", "origin", str(bare))
    git(calc_repo, "push", "-q", "origin", "main")
    git(calc_repo, "remote", "set-head", "origin", "main")
    q = JobQueue(config.db_path)
    job = q.submit(work_item)
    q.approve(job.id, "lead")
    job = Worker(config, q, provider_factory=lambda: ScriptedProvider(steps_fix_calc())).run_once()
    assert job.status == JobStatus.SUCCEEDED
    return q, job


@pytest.mark.parametrize("failure", ["status", "transport"])
def test_publish_github_failures_are_502_over_http(config, pushed_job, monkeypatch, failure):
    q, job = pushed_job
    monkeypatch.setenv("GITHUB_TOKEN", "ghp_test")
    monkeypatch.setenv("GCA_INBOUND_SECRET", "in")
    monkeypatch.setattr("gca.publish.github_slug", lambda url, host=None: "o/r")

    def api(req: httpx.Request) -> httpx.Response:
        if failure == "transport":
            raise httpx.ConnectError("boom", request=req)
        return httpx.Response(503, text="upstream down")

    monkeypatch.setattr("gca.publish.httpx.Client", lambda *a, **kw: httpx.Client(*a, **{**kw, "transport": httpx.MockTransport(api)}))
    config.webhooks = WebhookConfig(enabled=True)
    srv = make_server(config, q, "127.0.0.1", 0)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        base = f"http://127.0.0.1:{srv.server_address[1]}"
        path = f"/v1/jobs/{job.id}/publish"
        raw = json.dumps({"actor": "alice"}).encode()
        r = httpx.post(base + path, content=raw, headers=signed_headers("in", "POST", path, raw, delivery_id=f"p-{failure}"), timeout=10)
        assert r.status_code == 502 and "publish failed" in r.json()["error"]
        assert job.result.branch in git(job.work_item.repo_path, "ls-remote", "--heads", "origin")  # push did happen
    finally:
        srv.shutdown()
        srv.server_close()


def test_unexpected_handler_errors_still_answer_500(config, work_item, monkeypatch):
    monkeypatch.setenv("GCA_INBOUND_SECRET", "in")
    config.webhooks = WebhookConfig(enabled=True)
    q = JobQueue(config.db_path)
    srv = make_server(config, q, "127.0.0.1", 0)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    monkeypatch.setattr(CommandHandler, "handle", lambda *a, **kw: (_ for _ in ()).throw(RuntimeError("bug")))
    try:
        r = httpx.get(f"http://127.0.0.1:{srv.server_address[1]}/healthz", timeout=10)
        assert r.status_code == 500 and r.json()["error"].startswith("internal error")
    finally:
        srv.shutdown()
        srv.server_close()
        q.close()


# ---------------------------------------------------------------- 8 (P2) GitHub Enterprise remotes
@pytest.mark.parametrize("url,host,slug", [
    ("https://ghe.corp.example/org/repo.git", "ghe.corp.example", "org/repo"),
    ("git@ghe.corp.example:org/repo.git", "ghe.corp.example", "org/repo"),
    ("ssh://git@ghe.corp.example/org/repo", "ghe.corp.example", "org/repo"),
    ("https://github.com/org/repo", "ghe.corp.example", "org/repo"),  # github.com stays recognised
    ("https://gitlab.corp.example/org/repo.git", "ghe.corp.example", None),
    ("https://evilghe.corp.example/org/repo.git", "ghe.corp.example", None),  # suffix match is not a match
])
def test_enterprise_slug(url, host, slug):
    assert github_slug(url, host) == slug


def test_api_host():
    assert api_host("https://api.github.com") == "github.com"
    assert api_host("https://ghe.corp.example/api/v3") == "ghe.corp.example"
    assert api_host("") is None


def test_enterprise_publish_opens_the_pull_request(config, pushed_job, monkeypatch):
    q, job = pushed_job
    git(job.work_item.repo_path, "remote", "set-url", "origin", "https://ghe.corp.example/org/repo.git")
    git(job.work_item.repo_path, "remote", "set-url", "--push", "origin", str(Path(job.work_item.repo_path).parent / "remote.git"))
    monkeypatch.setenv("GHE_TOKEN", "ghe_token")
    seen = {}

    def api(req: httpx.Request) -> httpx.Response:
        seen["url"] = str(req.url)
        return httpx.Response(201, json={"html_url": "https://ghe.corp.example/org/repo/pull/3"})

    res = publish_job(config, q, job.id, token_env="GHE_TOKEN", api_base="https://ghe.corp.example/api/v3",
                      transport=httpx.MockTransport(api))
    assert res.pr_url == "https://ghe.corp.example/org/repo/pull/3"
    assert seen["url"] == "https://ghe.corp.example/api/v3/repos/org/repo/pulls"
    assert res.note == ""
    q.close()
