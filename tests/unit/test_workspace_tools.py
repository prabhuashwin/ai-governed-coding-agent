"""Git-worktree workspace isolation and the tool executor."""

from __future__ import annotations

import sys
from pathlib import Path

from conftest import CALC_FIX_NEW, CALC_FIX_OLD, git

from gca.config import PolicyConfig
from gca.policy import PolicyEngine
from gca.tools import ToolExecutor
from gca.workspace import GitWorktreeWorkspace


def make(calc_repo: Path, allowed=None):
    ws = GitWorktreeWorkspace(calc_repo, "job_t1")
    pol = PolicyEngine(PolicyConfig(allowed_commands=["python", Path(sys.executable).name, "ls"],
                                    canary_strings=["GCA-CANARY-7f3a9c"]), ws.root, allowed)
    ex = ToolExecutor(ws, pol, [sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider"])
    return ws, pol, ex


def test_worktree_isolates_original_checkout(calc_repo: Path):
    ws, _, ex = make(calc_repo)
    try:
        assert ws.root != calc_repo and ws.root.exists()
        assert ws.branch == "gca/job_t1"
        ex.execute("write_file", {"path": "new.txt", "content": "hi"})
        assert (ws.root / "new.txt").exists()
        assert not (calc_repo / "new.txt").exists()  # engineer's checkout untouched
        assert "new.txt" in ws.changed_files()
        assert "+hi" in ws.diff()
        sha = ws.commit_all("test commit")
        assert sha
        assert git(calc_repo, "branch", "--list", "gca/job_t1").strip()
    finally:
        ws.cleanup()
    assert not ws.root.exists()
    assert git(calc_repo, "branch", "--list", "gca/job_t1").strip()  # branch kept for review


def test_tools_read_search_edit_run(calc_repo: Path):
    ws, _, ex = make(calc_repo)
    try:
        listing = ex.execute("list_files", {"path": "."}).content
        assert "calc.py" in listing and "tests/test_calc.py" in listing and ".git" not in listing
        r = ex.execute("read_file", {"path": "calc.py", "start_line": 1, "end_line": 3})
        assert "1| " in r.content and "calculator" in r.content
        s = ex.execute("search", {"pattern": "BUG", "glob": "*.py"}).content
        assert s.startswith("calc.py:")
        before = ex.execute("run_tests", {}).content
        assert "[exit 1]" in before and "failed" in before
        e = ex.execute("edit_file", {"path": "calc.py", "old": CALC_FIX_OLD, "new": CALC_FIX_NEW})
        assert not e.is_error
        after = ex.execute("run_tests", {}).content
        assert "[exit 0]" in after
        amb = ex.execute("edit_file", {"path": "calc.py", "old": "return", "new": "x"})
        assert "times" in amb.content  # ambiguous
        miss = ex.execute("edit_file", {"path": "calc.py", "old": "nope-not-here", "new": "x"})
        assert "not found" in miss.content
    finally:
        ws.cleanup(keep_branch=False)


def test_tool_errors_do_not_raise(calc_repo: Path):
    ws, _, ex = make(calc_repo)
    try:
        assert ex.execute("read_file", {"path": "missing.py"}).is_error
        assert ex.execute("nope", {}).is_error
        assert ex.execute("read_file", {"wrong": 1}).is_error
        assert ex.execute("read_file", {"path": "tests"}).blocked  # directory
    finally:
        ws.cleanup(keep_branch=False)


def test_run_command_timeout_and_missing_binary(calc_repo: Path):
    ws, pol, ex = make(calc_repo)
    pol.cfg.command_timeout_seconds = 1
    try:
        r = ex.execute("run_command", {"argv": [sys.executable, "-c", "import time; time.sleep(5)"]})
        assert "timed out" in r.content and "[exit -1]" in r.content
        pol.cfg.allowed_commands.append("definitely-not-a-binary")
        r2 = ex.execute("run_command", {"argv": ["definitely-not-a-binary"]})
        assert "[exit 127]" in r2.content
    finally:
        ws.cleanup(keep_branch=False)


def test_output_truncation(calc_repo: Path):
    ws, pol, ex = make(calc_repo)
    pol.cfg.max_output_chars = 500
    try:
        r = ex.execute("run_command", {"argv": [sys.executable, "-c", "print('x' * 5000)"]})
        assert "truncated" in r.content and len(r.content) < 800
    finally:
        ws.cleanup(keep_branch=False)


def test_build_artifacts_never_reach_the_job_branch(calc_repo: Path):
    """A repo without a .gitignore: compiled caches created during the run must not be committed."""
    ws = GitWorktreeWorkspace(calc_repo, "job_art")
    try:
        (ws.root / "__pycache__").mkdir()
        (ws.root / "__pycache__" / "calc.cpython-313.pyc").write_bytes(b"\xf3\x0d\x0d\x0a binary")
        (ws.root / "tests" / "__pycache__").mkdir()
        (ws.root / "tests" / "__pycache__" / "t.pyc").write_bytes(b"\x00")
        (ws.root / ".pytest_cache").mkdir()
        (ws.root / ".pytest_cache" / "README.md").write_text("cache")
        assert ws.changed_files() == [] and ws.diff() == "" and ws.commit_all("nothing real") is None
        (ws.root / "calc.py").write_text((ws.root / "calc.py").read_text().replace(CALC_FIX_OLD, CALC_FIX_NEW))
        assert ws.changed_files() == ["calc.py"]
        sha = ws.commit_all("real change")
        files = git(calc_repo, "show", "--name-only", "--format=", sha).split()
        assert files == ["calc.py"]
    finally:
        ws.cleanup(keep_branch=False)


def test_line_endings_are_preserved(calc_repo: Path):
    """Editing must not rewrite a whole file's line endings (CRLF repos on Linux, LF repos on Windows)."""
    crlf = "def a():\r\n    return 1\r\n\r\n\r\ndef b():\r\n    return 2\r\n"
    (calc_repo / "win.py").write_bytes(crlf.encode())
    git(calc_repo, "add", "-A")
    git(calc_repo, "-c", "core.autocrlf=false", "commit", "-q", "-m", "crlf file")
    ws, _, ex = make(calc_repo)
    try:
        # model sends LF snippets (that is what it sees); the file stays CRLF and only one line changes
        r = ex.execute("edit_file", {"path": "win.py", "old": "def b():\n    return 2", "new": "def b():\n    return 3"})
        assert not r.is_error, r.content
        data = (ws.root / "win.py").read_bytes()
        assert data == crlf.replace("return 2", "return 3").encode()
        assert ws.diff_stat() == (1, 1)
        # calc.py keeps whatever style the checkout gave it (LF on Linux, CRLF with autocrlf on Windows)
        before = (ws.root / "calc.py").read_bytes()
        r = ex.execute("edit_file", {"path": "calc.py", "old": CALC_FIX_OLD, "new": CALC_FIX_NEW})
        assert not r.is_error, r.content
        after = (ws.root / "calc.py").read_bytes()
        assert after != before
        assert (b"\r\n" in after) == (b"\r\n" in before)
        assert after.count(b"\n") == before.count(b"\n")
        # overwriting a CRLF file with LF content keeps CRLF
        ex.execute("write_file", {"path": "win.py", "content": "x = 1\ny = 2\n"})
        assert (ws.root / "win.py").read_bytes() == b"x = 1\r\ny = 2\r\n"
        # new files are written exactly as given
        ex.execute("write_file", {"path": "new.py", "content": "a\nb\n"})
        assert (ws.root / "new.py").read_bytes() == b"a\nb\n"
    finally:
        ws.cleanup(keep_branch=False)


def test_rewritten_git_pointer_cannot_run_hooks_on_the_host(calc_repo: Path, tmp_path: Path):
    """Review finding 1: the worktree's `.git` file is agent-writable. Point it at attacker metadata with a
    hook; the next host commit must neither run the hook nor use that metadata."""
    ws, _, ex = make(calc_repo)
    try:
        marker = tmp_path / "hook-ran"
        evil = ws.root / "evilgit"
        git(ws.root, "init", "-q", str(evil))  # a complete .git directory under the worktree
        (evil / "x").write_text("x")
        git(evil, "add", "-A")
        git(evil, "commit", "-q", "-m", "attacker metadata")
        hooks = evil / ".git" / "hooks"
        hook = hooks / "pre-commit"
        hook.write_text(f"#!/bin/sh\necho pwned > '{marker}'\n")
        hook.chmod(0o755)
        (ws.root / ".git").write_text("gitdir: evilgit/.git\n")  # what the agent (or its code) would do
        ex.execute("edit_file", {"path": "calc.py", "old": CALC_FIX_OLD, "new": CALC_FIX_NEW})
        sha = ws.commit_all("fix")
        assert not marker.exists(), "hook from agent-controlled git metadata ran on the host"
        assert sha, "the real fix must still be committed"
        assert "evilgit" not in git(calc_repo, "show", "--stat", "--format=", "gca/job_t1")
        # the commit landed in the real repository, on the job branch
        assert sha in git(calc_repo, "rev-parse", "gca/job_t1")
        assert "calc.py" in git(calc_repo, "show", "--stat", "--format=", "gca/job_t1")
    finally:
        ws.cleanup()
