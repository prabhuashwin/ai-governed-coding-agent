"""Isolated workspaces.

`GitWorktreeWorkspace` checks the repo out at `base_ref` into a fresh git worktree on a new
branch `gca/<job_id>`, so the agent never touches the engineer's working copy. The interface is
small on purpose: a container-backed implementation can replace it without touching the agent.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol


@dataclass
class CommandResult:
    argv: list[str]
    exit_code: int
    stdout: str
    stderr: str
    timed_out: bool = False

    @property
    def output(self) -> str:
        return (self.stdout or "") + (("\n[stderr]\n" + self.stderr) if self.stderr else "")


class Workspace(Protocol):
    root: Path
    branch: str

    def run(self, argv: list[str], *, env: dict[str, str], timeout: int) -> CommandResult: ...
    def diff(self) -> str: ...
    def changed_files(self) -> list[str]: ...
    def diff_stat(self) -> tuple[int, int]: ...
    def commit_all(self, message: str) -> str | None: ...
    def cleanup(self) -> None: ...


def _git(repo: Path, *args: str, check: bool = True) -> subprocess.CompletedProcess:
    env = dict(os.environ)
    env.setdefault("GIT_AUTHOR_NAME", "gca")
    env.setdefault("GIT_AUTHOR_EMAIL", "gca@localhost")
    env.setdefault("GIT_COMMITTER_NAME", "gca")
    env.setdefault("GIT_COMMITTER_EMAIL", "gca@localhost")
    return subprocess.run(["git", *args], cwd=str(repo), capture_output=True, text=True, check=check, env=env)


class GitWorktreeWorkspace:
    def __init__(self, repo_path: str | Path, job_id: str, base_ref: str = "HEAD", parent_dir: str | None = None):
        self.repo = Path(repo_path).resolve()
        if not (self.repo / ".git").exists():
            raise ValueError(f"{self.repo} is not a git repository")
        self.branch = f"gca/{job_id}"
        self._tmp = Path(tempfile.mkdtemp(prefix=f"gca-{job_id}-", dir=parent_dir))
        self.root = self._tmp / "wt"
        base = _git(self.repo, "rev-parse", "--verify", base_ref).stdout.strip()
        self.base_commit = base
        # -B: reuse the branch name if a previous attempt left it behind
        _git(self.repo, "worktree", "add", "-B", self.branch, str(self.root), base)
        self.root = self.root.resolve()

    # ----------------------------------------------------------------- exec
    def run(self, argv: list[str], *, env: dict[str, str], timeout: int) -> CommandResult:
        try:
            cp = subprocess.run(
                argv, cwd=str(self.root), env=env, capture_output=True, text=True, timeout=timeout,
                stdin=subprocess.DEVNULL, errors="replace",
            )
            return CommandResult(argv, cp.returncode, cp.stdout, cp.stderr)
        except subprocess.TimeoutExpired as e:
            out = e.stdout.decode(errors="replace") if isinstance(e.stdout, bytes) else (e.stdout or "")
            err = e.stderr.decode(errors="replace") if isinstance(e.stderr, bytes) else (e.stderr or "")
            return CommandResult(argv, -1, out, err + f"\n[timed out after {timeout}s]", timed_out=True)
        except FileNotFoundError as e:
            return CommandResult(argv, 127, "", f"command not found: {e.filename}")

    # ------------------------------------------------------------------ git
    def diff(self) -> str:
        _git(self.root, "add", "-A", "--intent-to-add", check=False)
        return _git(self.root, "diff", "--no-color", self.base_commit).stdout

    def changed_files(self) -> list[str]:
        _git(self.root, "add", "-A", "--intent-to-add", check=False)
        out = _git(self.root, "diff", "--name-only", self.base_commit).stdout
        return [line for line in out.splitlines() if line.strip()]

    def diff_stat(self) -> tuple[int, int]:
        _git(self.root, "add", "-A", "--intent-to-add", check=False)
        out = _git(self.root, "diff", "--numstat", self.base_commit).stdout
        ins = dels = 0
        for line in out.splitlines():
            parts = line.split("\t")
            if len(parts) >= 2 and parts[0].isdigit() and parts[1].isdigit():
                ins += int(parts[0])
                dels += int(parts[1])
        return ins, dels

    def commit_all(self, message: str) -> str | None:
        _git(self.root, "add", "-A")
        if not _git(self.root, "status", "--porcelain").stdout.strip():
            return None
        _git(self.root, "commit", "-q", "-m", message)
        return _git(self.root, "rev-parse", "HEAD").stdout.strip()

    def cleanup(self, *, keep_branch: bool = True) -> None:
        _git(self.repo, "worktree", "remove", "--force", str(self.root), check=False)
        if not keep_branch:
            _git(self.repo, "branch", "-D", self.branch, check=False)
        shutil.rmtree(self._tmp, ignore_errors=True)
        _git(self.repo, "worktree", "prune", check=False)
