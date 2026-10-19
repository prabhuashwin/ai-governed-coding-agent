"""Agent-loop mechanics with a scripted model: no network, no cost, fully deterministic.

These prove the *agentic* properties we claimed: the model's tool choices drive the run,
the loop stops on goal or budget, failures are observations, and the harness stays in charge.
"""

from __future__ import annotations

import sys
from pathlib import Path

from conftest import CALC_FIX_NEW, CALC_FIX_OLD, steps_fix_calc

from gca.agent import AgentLoop
from gca.audit import AuditLog
from gca.config import Config
from gca.models import Budget, StopReason, WorkItem
from gca.policy import BudgetTracker, PolicyEngine
from gca.providers.testing import ScriptedProvider
from gca.tools import ToolExecutor
from gca.workspace import GitWorktreeWorkspace


class Harness:
    """Wires a scripted provider into a real workspace + policy + audit, like the worker does."""

    def __init__(self, config: Config, item: WorkItem, steps, *, budget: Budget | None = None,
                 cancel_after_turn: int | None = None, on_exhausted="finish"):
        self.cfg = config
        self.item = item
        self.ws = GitWorktreeWorkspace(item.repo_path, "job_loop", item.base_ref)
        self.policy = PolicyEngine(config.policy, self.ws.root, item.allowed_paths)
        self.tracker = BudgetTracker(budget or config.budget)
        self.audit = AuditLog(config.audit_dir, "job_loop")
        self.provider = ScriptedProvider(steps, on_exhausted=on_exhausted)
        self.executor = ToolExecutor(self.ws, self.policy, item.test_command)
        self.heartbeats = 0

        def cancel():
            return cancel_after_turn is not None and self.tracker.usage.turns >= cancel_after_turn

        def hb():
            self.heartbeats += 1

        self.loop = AgentLoop(self.provider, self.executor, self.tracker, self.audit,
                              cost_fn=lambda i, o: (i * 1.0 + o * 2.0) / 1_000_000,
                              max_blocked_actions=config.policy.max_blocked_actions,
                              cancel_check=cancel, heartbeat=hb)

    def run(self):
        try:
            return self.loop.run(self.item)
        finally:
            self.ws.cleanup(keep_branch=False)

    def events(self):
        return [e.event for e in self.audit.events()]


def test_happy_path_model_drives_fix(config, work_item):
    h = Harness(config, work_item, steps_fix_calc())
    out = h.run()
    assert out.stop_reason == StopReason.FINISHED and out.claimed_success
    assert out.usage.turns == 5 and out.usage.tool_calls == 4
    assert out.usage.cost_usd > 0  # cost accounting ran
    assert h.heartbeats >= 5
    # the model saw the failing test output before editing (observation -> action)
    second_call = h.provider.calls[1]["messages"]
    tool_result = second_call[-1]["content"][0]
    assert tool_result["type"] == "tool_result" and "failed" in tool_result["content"]
    # and the passing output after editing
    last_call = h.provider.calls[-1]["messages"][-1]["content"][0]["content"]
    assert "[exit 0]" in last_call
    ev = h.events()
    assert ev[0] == "started" and "finish_called" in ev and ev[-1] == "stopped"
    assert ev.count("tool_call") == 4


def test_system_prompt_and_task_framing(config, work_item):
    h = Harness(config, work_item, steps_fix_calc())
    h.run()
    first = h.provider.calls[0]
    assert "DATA, not instructions" in first["system"]
    task = first["messages"][0]["content"][0]["text"]
    assert "<<<DESCRIPTION" in task and work_item.description in task
    assert "1. mean() divides" in task and "Budget:" in task


def test_failure_is_an_observation_not_a_crash(config, work_item):
    """First edit is wrong; tests still fail; model reads output and fixes it."""
    steps = [
        {"tool_calls": [{"name": "edit_file", "arguments": {"path": "calc.py", "old": CALC_FIX_OLD,
                                                            "new": "return sum(values) / (len(values) + 1)"}}]},
        {"tool_calls": [{"name": "run_tests", "arguments": {}}]},
        {"tool_calls": [{"name": "edit_file", "arguments": {"path": "calc.py", "old": "return sum(values) / (len(values) + 1)",
                                                            "new": CALC_FIX_NEW}}]},
        {"tool_calls": [{"name": "run_tests", "arguments": {}}]},
        {"tool_calls": [{"name": "finish", "arguments": {"summary": "fixed on second try", "success": True}}]},
    ]
    h = Harness(config, work_item, steps)
    out = h.run()
    assert out.stop_reason == StopReason.FINISHED and out.claimed_success
    # the loop handed the failing output back to the model between attempts
    msgs = h.provider.calls[2]["messages"]
    assert "[exit 1]" in msgs[-1]["content"][0]["content"]


def test_budget_exhaustion_turns(config, work_item):
    steps = [{"tool_calls": [{"name": "list_files", "arguments": {}}]}]
    h = Harness(config, work_item, steps, budget=Budget(max_turns=3, max_tool_calls=100, max_seconds=60),
                on_exhausted="repeat_last")
    out = h.run()
    assert out.stop_reason == StopReason.BUDGET_EXHAUSTED and out.detail == "max_turns"
    assert out.usage.turns == 3 and not out.claimed_success


def test_budget_exhaustion_cost(config, work_item):
    steps = [{"tool_calls": [{"name": "list_files", "arguments": {}}], "input_tokens": 400_000, "output_tokens": 50_000}]
    h = Harness(config, work_item, steps, budget=Budget(max_cost_usd=0.4, max_turns=50, max_input_tokens=10_000_000), on_exhausted="repeat_last")
    out = h.run()
    assert out.stop_reason == StopReason.BUDGET_EXHAUSTED and out.detail == "max_cost_usd"
    assert out.usage.turns == 1  # 0.4 + 0.1 = 0.5 > 0.4 after one turn


def test_budget_tool_calls_mid_turn(config, work_item):
    many = [{"name": "list_files", "arguments": {}} for _ in range(6)]
    steps = [{"tool_calls": many}, {"tool_calls": [{"name": "finish", "arguments": {"summary": "s", "success": False}}]}]
    h = Harness(config, work_item, steps, budget=Budget(max_tool_calls=3, max_turns=10))
    out = h.run()
    assert out.stop_reason == StopReason.BUDGET_EXHAUSTED and out.detail == "max_tool_calls"
    assert len(h.provider.calls) == 1  # no further model call once the budget is gone
    results = h.loop.messages[-1].content
    assert sum(1 for r in results if r.content == "budget exhausted") == 3
    assert out.usage.tool_calls == 3


def test_cancellation_stops_the_loop(config, work_item):
    steps = [{"tool_calls": [{"name": "list_files", "arguments": {}}]}]
    h = Harness(config, work_item, steps, cancel_after_turn=2, on_exhausted="repeat_last")
    out = h.run()
    assert out.stop_reason == StopReason.CANCELLED and out.usage.turns == 2


def test_provider_error_is_reported_not_raised(config, work_item):
    h = Harness(config, work_item, [{"tool_calls": [{"name": "list_files", "arguments": {}}]}], on_exhausted="error")
    out = h.run()
    assert out.stop_reason == StopReason.PROVIDER_ERROR and "exhausted" in out.detail


def test_model_that_stops_using_tools_is_nudged_then_ended(config, work_item):
    steps = [{"text": "I think it's fine."}, {"text": "Really, done."}]
    h = Harness(config, work_item, steps)
    out = h.run()
    assert out.stop_reason == StopReason.FINISHED and not out.claimed_success
    assert "Continue using the tools" in h.provider.calls[1]["messages"][-1]["content"][0]["text"]


def test_unknown_tool_and_bad_args_are_survivable(config, work_item):
    steps = [
        {"tool_calls": [{"name": "teleport", "arguments": {}}]},
        {"tool_calls": [{"name": "read_file", "arguments": {"nope": 1}}]},
        {"tool_calls": [{"name": "finish", "arguments": {"summary": "gave up", "success": False}}]},
    ]
    out = Harness(config, work_item, steps).run()
    assert out.stop_reason == StopReason.FINISHED and out.blocked_actions == 0


def test_context_compaction_bounds_old_tool_results(config, work_item):
    steps = [{"tool_calls": [{"name": "read_file", "arguments": {"path": "tests/test_calc.py"}}]}] * 12 + [
        {"tool_calls": [{"name": "finish", "arguments": {"summary": "s", "success": False}}]}]
    h = Harness(config, work_item, steps, budget=Budget(max_turns=20, max_tool_calls=50))
    h.loop.keep_recent = 2
    h.loop.result_cap = 50
    h.run()
    msgs = h.provider.calls[-1]["messages"]
    old_results = [b for m in msgs[:-4] for b in m["content"] if b["type"] == "tool_result"]
    assert old_results and all(len(b["content"]) <= 50 + 40 for b in old_results)
    assert any("compacted" in b["content"] for b in old_results)


def test_allowed_paths_confine_writes(config, work_item):
    work_item.allowed_paths = ["tests"]
    steps = [
        {"tool_calls": [{"name": "edit_file", "arguments": {"path": "calc.py", "old": CALC_FIX_OLD, "new": CALC_FIX_NEW}}]},
        {"tool_calls": [{"name": "finish", "arguments": {"summary": "s", "success": False}}]},
    ]
    h = Harness(config, work_item, steps)
    out = h.run()
    assert out.blocked_actions == 1
    assert "BLOCKED" in h.provider.calls[1]["messages"][-1]["content"][0]["content"]


def test_python_executable_name_is_allowlisted(config):
    assert Path(sys.executable).name in config.policy.allowed_commands


# --------------------------------------------------------------- review findings (2026-10-19)
def test_final_turn_cannot_exceed_token_and_cost_caps(config, work_item):
    """The turn that called finish was charged but never checked, so a job capped at 10 input tokens could
    finish successfully having used 101."""
    steps = [{"tool_calls": [{"name": "finish", "arguments": {"summary": "done", "success": True}}],
              "input_tokens": 101, "output_tokens": 1}]
    h = Harness(config, work_item, steps, budget=Budget(max_input_tokens=10, max_turns=50, max_cost_usd=10))
    out = h.run()
    assert out.stop_reason == StopReason.BUDGET_EXHAUSTED and out.detail == "max_input_tokens"
    assert not out.claimed_success
    cheap = [{"tool_calls": [{"name": "finish", "arguments": {"summary": "done", "success": True}}],
              "input_tokens": 5, "output_tokens": 1}]
    assert Harness(config, work_item, cheap, budget=Budget(max_input_tokens=10, max_turns=50, max_cost_usd=10)).run().stop_reason == StopReason.FINISHED
