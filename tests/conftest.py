"""Shared fixtures: a tiny git repository with a seeded bug and a failing test, plus config helpers."""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

from gca.config import Config, PolicyConfig
from gca.models import Budget, WorkItem

GIT_ENV = {
    "GIT_AUTHOR_NAME": "test", "GIT_AUTHOR_EMAIL": "test@localhost",
    "GIT_COMMITTER_NAME": "test", "GIT_COMMITTER_EMAIL": "test@localhost",
}

CALC_SRC = '''"""Tiny calculator module with a seeded off-by-one bug."""


def add(a, b):
    return a + b


def mean(values):
    if not values:
        raise ValueError("mean of empty list")
    return sum(values) / (len(values) - 1)  # BUG: should divide by len(values)
'''

CALC_TEST = '''from calc import add, mean
import pytest


def test_add():
    assert add(2, 3) == 5


def test_mean():
    assert mean([2, 4, 6]) == 4


def test_mean_empty():
    with pytest.raises(ValueError):
        mean([])
'''

CALC_FIX_OLD = "return sum(values) / (len(values) - 1)  # BUG: should divide by len(values)"
CALC_FIX_NEW = "return sum(values) / len(values)"


def git(repo: Path, *args: str) -> str:
    env = {**os.environ, **GIT_ENV}
    cp = subprocess.run(["git", *args], cwd=str(repo), capture_output=True, text=True, env=env)
    assert cp.returncode == 0, cp.stderr
    return cp.stdout


def make_repo(root: Path, files: dict[str, str]) -> Path:
    root.mkdir(parents=True, exist_ok=True)
    for rel, content in files.items():
        p = root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content, encoding="utf-8")
    git(root, "init", "-q", "-b", "main")
    git(root, "add", "-A")
    git(root, "commit", "-q", "-m", "fixture")
    return root


@pytest.fixture
def calc_repo(tmp_path: Path) -> Path:
    """A repo whose tests fail because of the seeded bug in calc.mean."""
    return make_repo(tmp_path / "repo", {
        "calc.py": CALC_SRC,
        "tests/test_calc.py": CALC_TEST,
        "tests/__init__.py": "",
        "conftest.py": "import sys, pathlib\nsys.path.insert(0, str(pathlib.Path(__file__).parent))\n",
    })


@pytest.fixture
def work_item(calc_repo: Path) -> WorkItem:
    return WorkItem(
        title="Fix mean() off-by-one",
        description="mean([2, 4, 6]) returns 6 instead of 4. Fix the division in calc.mean.",
        acceptance_criteria=["mean() divides by the number of values", "all tests pass"],
        repo_path=str(calc_repo),
        test_command=[sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider"],
        requester="tester",
    )


@pytest.fixture
def config(tmp_path: Path) -> Config:
    cfg = Config(data_dir=str(tmp_path / ".gca"))
    # allow the current interpreter so run_tests works in any venv
    cfg.policy = PolicyConfig(allowed_commands=sorted(set(cfg.policy.allowed_commands) | {Path(sys.executable).name}),
                              canary_strings=["GCA-CANARY-7f3a9c"])
    cfg.budget = Budget(max_turns=12, max_tool_calls=40, max_cost_usd=1.0, max_seconds=120)
    # pricing so cost accounting is exercised
    cfg.pricing.input_per_million = {"scripted": 1.0}
    cfg.pricing.output_per_million = {"scripted": 2.0}
    return cfg


# --- scripted "model" behaviours -----------------------------------------------------------
def steps_fix_calc(run_tests_first: bool = True) -> list[dict]:
    steps = []
    if run_tests_first:
        steps.append({"text": "Let me look at the failing tests.", "tool_calls": [{"name": "run_tests", "arguments": {}}]})
    steps += [
        {"tool_calls": [{"name": "read_file", "arguments": {"path": "calc.py"}}]},
        {"tool_calls": [{"name": "edit_file", "arguments": {"path": "calc.py", "old": CALC_FIX_OLD, "new": CALC_FIX_NEW}}]},
        {"tool_calls": [{"name": "run_tests", "arguments": {}}]},
        {"text": "Tests pass.", "tool_calls": [{"name": "finish", "arguments": {"summary": "Fixed the divisor in mean().", "success": True}}]},
    ]
    return steps
