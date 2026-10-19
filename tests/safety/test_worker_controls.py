"""Work package 2: structured approval restrictions, interruptible execution, publishing via webhook, stop scopes."""

from __future__ import annotations

import sys
import threading
import time

import pytest
from conftest import CALC_FIX_NEW, CALC_FIX_OLD, git, steps_fix_calc

from gca.models import JobStatus, Restrictions, StopReason
from gca.providers.testing import ScriptedProvider
from gca.queue import JobQueue
from gca.worker import Worker

pytestmark = pytest.mark.safety


def _approve(q, job_id, **restr):
    return q.approve(job_id, "lead", restrictions=Restrictions(**restr), operator_commands=["python", "python3",
                                                                                           sys.executable.split("/")[-1].split("\\\\")[-1], "pytest"])


# ----------------------------------------------------------------------- restrictions
def test_approval_can_only_narrow_paths(config, work_item, calc_repo):
    q = JobQueue(config.db_path)
    work_item.allowed_paths = ["src"]
    job = q.submit(work_item)
    with pytest.raises(ValueError, match="outside the work item's allowed paths"):
        _approve(q, job.id, allowed_paths=["tests"])
    job = _approve(q, job.id, allowed_paths=["src/calc"])
    assert job.work_item.allowed_paths == ["src/calc"] and "paths limited" in job.approval.note


def test_approval_cannot_raise_budget_or_add_commands(config, work_item):
    q = JobQueue(config.db_path)
    job = q.submit(work_item)
    with pytest.raises(ValueError, match="would raise"):
        _approve(q, job.id, budget={"max_cost_usd": 999})
    with pytest.raises(ValueError, match="not in the operator's command allow-list"):
        _approve(q, job.id, allowed_commands=["curl"])
    job = _approve(q, job.id, budget={"max_turns": 3, "max_cost_usd": 0.05})
    assert job.budget.max_turns == 3 and job.budget.max_cost_usd == 0.05


def test_restricted_paths_are_enforced_by_tools_and_verifier(config, work_item, calc_repo):
    """The approver limits writes to `allowed/`; the agent edits calc.py anyway (file tool) and the job fails."""
    (calc_repo / "allowed").mkdir()
    (calc_repo / "allowed" / "note.txt").write_text("x\n")
    git(calc_repo, "add", "-A")
    git(calc_repo, "commit", "-q", "-m", "allowed dir")
    q = JobQueue(config.db_path)
    job = q.submit(work_item)
    _approve(q, job.id, allowed_paths=["allowed"])
    job = Worker(config, q, provider_factory=lambda: ScriptedProvider(steps_fix_calc())).run_once()
    assert job.status == JobStatus.FAILED
    assert job.result.agent.blocked_actions >= 1  # the edit_file on calc.py was refused by policy
    assert "Approval restrictions (enforced)" in job.result.report_markdown


def test_restricted_commands_are_enforced(config, work_item):
    """The approver allows only `pytest`; the agent's python command is blocked, run_tests still works."""
    q = JobQueue(config.db_path)
    job = q.submit(work_item)
    q.approve(job.id, "lead", restrictions=Restrictions(allowed_commands=["pytest"]),
              operator_commands=config.policy.allowed_commands)
    steps = [
        {"tool_calls": [{"name": "run_command", "arguments": {"argv": [sys.executable, "-c", "print(1)"]}}]},
        {"tool_calls": [{"name": "edit_file", "arguments": {"path": "calc.py", "old": CALC_FIX_OLD, "new": CALC_FIX_NEW}}]},
        {"tool_calls": [{"name": "finish", "arguments": {"summary": "done", "success": True}}]},
    ]
    job = Worker(config, q, provider_factory=lambda: ScriptedProvider(steps)).run_once()
    assert job.result.agent.blocked_actions == 1 and job.status == JobStatus.FAILED  # one blocked action fails the job


# ----------------------------------------------------------------------- interruption
def test_cancel_interrupts_a_running_command(config, work_item):
    """A command that would run for 20 s is stopped within a couple of seconds of a cancel request."""
    q = JobQueue(config.db_path)
    job = q.submit(work_item)
    q.approve(job.id, "lead")
    steps = [{"tool_calls": [{"name": "run_command", "arguments": {"argv": [sys.executable, "-c", "import time; time.sleep(20)"]}}]},
             {"tool_calls": [{"name": "finish", "arguments": {"summary": "done", "success": True}}]}]

    def cancel_soon():
        time.sleep(1.0)
        JobQueue(config.db_path).request_cancel(job.id)

    threading.Thread(target=cancel_soon).start()
    t0 = time.monotonic()
    job = Worker(config, q, provider_factory=lambda: ScriptedProvider(steps)).run_once()
    assert time.monotonic() - t0 < 8, "the running command was not interrupted"
    assert job.status == JobStatus.CANCELLED and job.result.agent.stop_reason == StopReason.CANCELLED
    assert job.result.agent.detail == "command interrupted"


def test_kill_switch_interrupts_verification(config, work_item, monkeypatch):
    """The verifier's test run is in flight when the kill switch is engaged: it stops and the job is cancelled
    with kill_switch, not left running until the test command finishes."""
    q = JobQueue(config.db_path)
    work_item.test_command = [sys.executable, "-c", "import time; time.sleep(20)"]
    job = q.submit(work_item)
    q.approve(job.id, "lead")
    steps = [{"tool_calls": [{"name": "edit_file", "arguments": {"path": "calc.py", "old": CALC_FIX_OLD, "new": CALC_FIX_NEW}}]},
             {"tool_calls": [{"name": "finish", "arguments": {"summary": "done", "success": True}}]}]

    def kill_soon():
        time.sleep(1.5)
        JobQueue(config.db_path).set_kill_switch(True, reason="drill", actor="secops")

    threading.Thread(target=kill_soon).start()
    t0 = time.monotonic()
    job = Worker(config, q, provider_factory=lambda: ScriptedProvider(steps)).run_once()
    assert time.monotonic() - t0 < 10
    assert job.status == JobStatus.CANCELLED and job.error == "kill_switch"
    assert job.result.verification.flags == ["interrupted"]
    assert "status **cancelled**" in job.result.report_markdown
