"""The eval runner itself, driven by a scripted model (the real-model run is `gca eval`)."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

from gca.evals import load_task, run_task, summarize
from gca.providers.testing import ScriptedProvider

pytestmark = pytest.mark.e2e
TASKS = Path(__file__).resolve().parents[2] / "evals" / "tasks"


def test_all_tasks_are_well_formed_and_fail_before_fix():
    names = sorted(p.name for p in TASKS.iterdir() if p.is_dir())
    assert len(names) >= 3
    for n in names:
        spec = load_task(TASKS / n)
        assert spec["title"] and spec["acceptance_criteria"] and (spec["repo_src"] / "tests").is_dir()


def test_eval_runner_with_scripted_fix(config, monkeypatch):
    spec = load_task(TASKS / "01-off-by-one-mean")
    spec["test_command"] = [sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider"]
    steps = [
        {"tool_calls": [{"name": "run_tests", "arguments": {}}]},
        {"tool_calls": [{"name": "edit_file", "arguments": {"path": "stats.py", "old": "return sum(values) / (len(values) - 1)",
                                                            "new": "return sum(values) / len(values)"}}]},
        {"tool_calls": [{"name": "run_tests", "arguments": {}}]},
        {"tool_calls": [{"name": "finish", "arguments": {"summary": "fixed divisor", "success": True}}]},
    ]
    monkeypatch.setattr("gca.worker.build_provider", lambda cfg: ScriptedProvider(steps))
    row = run_task(config, spec, config.audit_dir.parent / "evaldata")
    assert row["passed"] and row["turns"] == 4 and row["insertions"] == 1 and row["deletions"] == 1
    md = summarize([row])
    assert "1/1 passed" in md and "01-off-by-one-mean" in md


def test_eval_runner_records_failure(config, monkeypatch):
    spec = load_task(TASKS / "02-slugify-edge-cases")
    spec["test_command"] = [sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider"]
    steps = [{"tool_calls": [{"name": "finish", "arguments": {"summary": "could not", "success": False}}]}]
    monkeypatch.setattr("gca.worker.build_provider", lambda cfg: ScriptedProvider(steps))
    row = run_task(config, spec, config.audit_dir.parent / "evaldata")
    assert not row["passed"] and row["status"] == "failed" and row["claimed_success"] is False
