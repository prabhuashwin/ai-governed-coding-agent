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


# ----------------------------------------------------------------------------- containers
def container_run_argv(*, runtime: str, image: str, host_dir: Path, argv: list[str], env: dict[str, str],
                       name: str, network: str = "none", memory: str = "2g", cpus: str = "2",
                       pids_limit: int = 256, user: str | None = None, extra_args: list[str] | None = None,
                       workdir: str = "/work") -> list[str]:
    """Build the `docker run` / `podman run` command line. Pure, so it can be unit-tested exactly.

    Hardening defaults: no network, dropped capabilities, no privilege escalation, memory/CPU/pid
    limits, a tmpfs /tmp, and the repository bind-mounted at /work as the only writable host path.
    """
    cmd = [runtime, "run", "--rm", "--name", name, f"--network={network}", "--cap-drop=ALL",
           "--security-opt=no-new-privileges", f"--memory={memory}", f"--cpus={cpus}",
           f"--pids-limit={pids_limit}", "--tmpfs", "/tmp:rw,noexec,nosuid,size=256m",
           "-v", f"{host_dir}:{workdir}", "-w", workdir]
    if user:
        cmd += ["--user", user]
    for k, v in sorted(env.items()):
        if k in ("PATH", "HOME"):  # the image defines these
            continue
        cmd += ["-e", f"{k}={v}"]
    cmd += ["-e", f"HOME={workdir}/.gca-home"]
    cmd += list(extra_args or [])
    cmd.append(image)
    cmd += list(argv)
    return cmd


def detect_runtime(preferred: str = "docker") -> str | None:
    """Return the first available container runtime binary name, or None."""
    for cand in [preferred] + [r for r in ("docker", "podman") if r != preferred]:
        if shutil.which(cand):
            return cand
    return None


def run_in_container(*, runtime: str, image: str, host_dir: Path, argv: list[str], env: dict[str, str], timeout: int,
                     name: str, network: str = "none", memory: str = "2g", cpus: str = "2", pids_limit: int = 256,
                     user: str | None = None, extra_args: list[str] | None = None) -> CommandResult:
    cmd = container_run_argv(runtime=runtime, image=image, host_dir=host_dir, argv=argv, env=env, name=name,
                             network=network, memory=memory, cpus=cpus, pids_limit=pids_limit, user=user,
                             extra_args=extra_args)
    (Path(host_dir) / ".gca-home").mkdir(exist_ok=True)
    try:
        cp = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, stdin=subprocess.DEVNULL,
                            errors="replace")
        return CommandResult(argv, cp.returncode, cp.stdout, cp.stderr)
    except subprocess.TimeoutExpired as e:
        subprocess.run([runtime, "kill", name], capture_output=True)
        out = e.stdout.decode(errors="replace") if isinstance(e.stdout, bytes) else (e.stdout or "")
        err = e.stderr.decode(errors="replace") if isinstance(e.stderr, bytes) else (e.stderr or "")
        return CommandResult(argv, -1, out, err + f"\n[timed out after {timeout}s; container killed]", timed_out=True)
    except FileNotFoundError as e:
        return CommandResult(argv, 127, "", f"container runtime not found: {e.filename}")


def host_user() -> str | None:
    if hasattr(os, "getuid"):
        return f"{os.getuid()}:{os.getgid()}"  # type: ignore[attr-defined]
    return None


class ContainerWorkspace(GitWorktreeWorkspace):
    """Git worktree on the host for git operations; every `run()` executes inside a throwaway container.

    The agent still only ever sees paths relative to the repository root, which is bind-mounted at
    /work. Git (diff/commit) runs on the host, so the image needs no git and no credentials.
    """

    def __init__(self, repo_path: str | Path, job_id: str, base_ref: str = "HEAD", *, image: str,
                 runtime: str = "docker", network: str = "none", memory: str = "2g", cpus: str = "2",
                 pids_limit: int = 256, extra_args: list[str] | None = None, run_as_host_user: bool = True,
                 parent_dir: str | None = None):
        rt = detect_runtime(runtime)
        if rt is None:
            raise RuntimeError(f"container runtime {runtime!r} not found on PATH (install Docker or Podman, "
                               f"or set [workspace] kind = \"worktree\")")
        super().__init__(repo_path, job_id, base_ref, parent_dir=parent_dir)
        self.runtime, self.image, self.network = rt, image, network
        self.memory, self.cpus, self.pids_limit = memory, cpus, pids_limit
        self.extra_args = list(extra_args or [])
        self.user = host_user() if run_as_host_user else None
        self._n = 0
        self._job_id = job_id

    def run(self, argv: list[str], *, env: dict[str, str], timeout: int) -> CommandResult:
        self._n += 1
        return run_in_container(runtime=self.runtime, image=self.image, host_dir=self.root, argv=argv, env=env,
                                timeout=timeout, name=f"gca-{self._job_id}-{self._n}", network=self.network,
                                memory=self.memory, cpus=self.cpus, pids_limit=self.pids_limit, user=self.user,
                                extra_args=self.extra_args)

    def changed_files(self) -> list[str]:
        return [f for f in super().changed_files() if not f.startswith(".gca-home/")]

    def diff(self) -> str:
        # exclude the scratch HOME the container uses
        _git(self.root, "add", "-A", "--intent-to-add", check=False)
        return _git(self.root, "diff", "--no-color", self.base_commit, "--", ".", ":(exclude).gca-home").stdout

    def commit_all(self, message: str) -> str | None:
        shutil.rmtree(self.root / ".gca-home", ignore_errors=True)
        return super().commit_all(message)


# ----------------------------------------------------------------------------- factory
def make_workspace(ws_cfg, repo_path: str | Path, job_id: str, base_ref: str = "HEAD") -> GitWorktreeWorkspace:
    """Build the configured workspace kind. `ws_cfg` is `config.WorkspaceConfig`."""
    if ws_cfg.kind == "container":
        return ContainerWorkspace(repo_path, job_id, base_ref, image=ws_cfg.image, runtime=ws_cfg.runtime,
                                  network=ws_cfg.network, memory=ws_cfg.memory, cpus=ws_cfg.cpus,
                                  pids_limit=ws_cfg.pids_limit, extra_args=ws_cfg.extra_args,
                                  run_as_host_user=ws_cfg.run_as_host_user)
    if ws_cfg.kind == "worktree":
        return GitWorktreeWorkspace(repo_path, job_id, base_ref)
    raise ValueError(f"unknown workspace kind: {ws_cfg.kind}")


def make_runner(ws_cfg, job_id: str):
    """A (argv, cwd, env, timeout) -> CommandResult callable matching the workspace kind; used by the verifier
    so verification happens under the same isolation as the agent run."""
    if ws_cfg.kind != "container":
        def host_run(argv: list[str], cwd: Path, env: dict[str, str], timeout: int) -> CommandResult:
            try:
                cp = subprocess.run(argv, cwd=str(cwd), env=env, capture_output=True, text=True, timeout=timeout,
                                    stdin=subprocess.DEVNULL, errors="replace")
                return CommandResult(argv, cp.returncode, cp.stdout, cp.stderr)
            except subprocess.TimeoutExpired:
                return CommandResult(argv, -1, "", f"[timed out after {timeout}s]", timed_out=True)
            except FileNotFoundError as e:
                return CommandResult(argv, 127, "", f"command not found: {e.filename}")
        return host_run

    runtime = detect_runtime(ws_cfg.runtime)
    if runtime is None:
        raise RuntimeError(f"container runtime {ws_cfg.runtime!r} not found on PATH")
    counter = {"n": 0}

    def container_run(argv: list[str], cwd: Path, env: dict[str, str], timeout: int) -> CommandResult:
        counter["n"] += 1
        return run_in_container(runtime=runtime, image=ws_cfg.image, host_dir=Path(cwd), argv=argv, env=env,
                                timeout=timeout, name=f"gca-verify-{job_id}-{counter['n']}", network=ws_cfg.network,
                                memory=ws_cfg.memory, cpus=ws_cfg.cpus, pids_limit=ws_cfg.pids_limit,
                                user=host_user() if ws_cfg.run_as_host_user else None, extra_args=ws_cfg.extra_args)
    return container_run
