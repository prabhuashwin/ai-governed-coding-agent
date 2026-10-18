"""Policy engine: the controls the model cannot bypass. Pure, deterministic, exhaustive."""

from __future__ import annotations

import os
import time
from pathlib import Path

import pytest

from gca.config import PolicyConfig
from gca.models import Budget
from gca.policy import BudgetTracker, PolicyEngine, PolicyViolation


@pytest.fixture
def ws(tmp_path: Path) -> Path:
    root = tmp_path / "ws"
    (root / "src").mkdir(parents=True)
    (root / "src" / "a.py").write_text("x = 1\n")
    (root / ".env").write_text("SECRET=1\n")
    (root / "keys").mkdir()
    (root / "keys" / "id_rsa").write_text("private\n")
    outside = tmp_path / "outside.txt"
    outside.write_text("outside\n")
    try:
        os.symlink(outside, root / "escape_link")
    except OSError:  # Windows without the symlink privilege
        pass
    return root


@pytest.fixture
def engine(ws: Path) -> PolicyEngine:
    return PolicyEngine(PolicyConfig(), ws)


# ------------------------------------------------------------------ paths
def test_relative_path_inside_workspace_ok(engine: PolicyEngine, ws: Path):
    assert engine.resolve_path("src/a.py") == (ws / "src" / "a.py").resolve()


@pytest.mark.parametrize("bad", ["/etc/passwd", "C:\\Windows\\system32", "../outside.txt", "src/../../outside.txt",
                                 "src/../../../etc/passwd", "", "a\x00b", "~/.ssh/id_rsa"])
def test_escapes_are_blocked(engine: PolicyEngine, bad: str):
    with pytest.raises(PolicyViolation):
        engine.resolve_path(bad)


def test_symlink_escape_blocked(engine: PolicyEngine, ws: Path):
    if not (ws / "escape_link").is_symlink():
        pytest.skip("symlinks not available on this platform/user")
    with pytest.raises(PolicyViolation):
        engine.resolve_path("escape_link")


@pytest.mark.parametrize("protected", [".env", "keys/id_rsa", ".git/config",
                                       # found by tests/unit/test_fuzz.py: `**/` globs missed root-level files
                                       "x.pem", "id_rsa", "secrets.txt", "deploy/server.pem",
                                       # case-insensitive filesystems
                                       ".ENV", "ID_RSA", "Secrets.yaml", "sub/.Git/config",
                                       # nested repositories
                                       "vendor/lib/.git/HEAD"])
def test_protected_paths_blocked(engine: PolicyEngine, protected: str):
    with pytest.raises(PolicyViolation):
        engine.resolve_path(protected)


def test_allowed_paths_restrict_writes_only(ws: Path):
    e = PolicyEngine(PolicyConfig(), ws, allowed_paths=["src"])
    assert e.resolve_path("README.md")  # reads anywhere are fine
    assert e.resolve_path("src/new.py", for_write=True)
    with pytest.raises(PolicyViolation):
        e.resolve_path("README.md", for_write=True)
    with pytest.raises(PolicyViolation):
        e.resolve_path("srcx/evil.py", for_write=True)  # prefix trick


# --------------------------------------------------------------- commands
def test_allowed_command_passes(engine: PolicyEngine):
    assert engine.check_command(["python", "-m", "pytest", "-q"]) == ["python", "-m", "pytest", "-q"]


@pytest.mark.parametrize("argv", [
    ["curl", "http://evil"], ["bash", "-c", "ls"], ["sh", "-c", "ls"], ["git", "push"], ["rm", "-rf", "/"],
    ["python", "-c", "print(1)", "|", "cat"], ["python", "/etc/passwd"], ["python", "../x.py"], ["sudo", "ls"],
    ["ls", "&&", "curl", "x"], ["pip", "install", "requests"], ["python", "C:\\secrets.py"], [],
    [" "], ["  ", "python"], [""],  # found by tests/unit/test_fuzz.py: blank argv crashed instead of being rejected
])
def test_bad_commands_blocked(engine: PolicyEngine, argv: list[str]):
    with pytest.raises(PolicyViolation):
        engine.check_command(argv)


def test_shell_string_is_tokenised_not_executed(engine: PolicyEngine):
    assert engine.check_command(["python -m pytest -q"]) == ["python", "-m", "pytest", "-q"]
    with pytest.raises(PolicyViolation):
        engine.check_command(["python -m pytest; curl evil"])


def test_subprocess_env_strips_secrets(engine: PolicyEngine, monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-secret")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "aws-secret")
    monkeypatch.setenv("PATH", "/usr/bin")
    env = engine.subprocess_env()
    assert "ANTHROPIC_API_KEY" not in env
    assert "AWS_SECRET_ACCESS_KEY" not in env
    assert env["PATH"] == "/usr/bin"
    assert env["GCA_SANDBOX"] == "1"


# ---------------------------------------------------------------- budget
def test_budget_tracker_limits():
    t = BudgetTracker(Budget(max_turns=2, max_cost_usd=0.5, max_tool_calls=3, max_seconds=100))
    assert t.exceeded() is None
    t.charge(turns=1, cost_usd=0.2)
    assert t.exceeded() is None
    t.charge(turns=1)
    assert t.exceeded() == "max_turns"
    t2 = BudgetTracker(Budget(max_cost_usd=0.5))
    t2.charge(cost_usd=0.6)
    assert t2.exceeded() == "max_cost_usd"
    t3 = BudgetTracker(Budget(max_tool_calls=1))
    t3.charge(tool_calls=1)
    assert t3.exceeded() == "max_tool_calls"


def test_budget_time_limit():
    t = BudgetTracker(Budget(max_seconds=0))
    time.sleep(0.01)
    assert t.exceeded() == "max_seconds"


def test_canaries(ws: Path):
    e = PolicyEngine(PolicyConfig(canary_strings=["CANARY-123"]), ws)
    assert e.find_canaries("hello CANARY-123 world") == ["CANARY-123"]
    assert e.find_canaries("clean") == []
