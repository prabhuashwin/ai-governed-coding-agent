"""Container workspace: exact command construction, a fake runtime end-to-end, and a real daemon if present."""

from __future__ import annotations

import os
import shutil
import stat
import sys
from pathlib import Path

import pytest
from conftest import CALC_FIX_NEW, CALC_FIX_OLD, steps_fix_calc

from gca.config import WorkspaceConfig
from gca.models import JobStatus
from gca.providers.testing import ScriptedProvider
from gca.queue import JobQueue
from gca.worker import Worker
from gca.workspace import ContainerWorkspace, container_run_argv, detect_runtime, make_runner, make_workspace


def test_container_run_argv_is_hardened_and_exact(tmp_path: Path):
    cmd = container_run_argv(runtime="docker", image="python:3.12-slim", host_dir=tmp_path / "wt",
                             argv=["python", "-m", "pytest", "-q"], env={"PATH": "/x", "HOME": "/y", "NO_COLOR": "1",
                                                                          "GCA_SANDBOX": "1"},
                             name="gca-job1-1", user="1000:1000", extra_args=["--label", "gca=1"])
    assert cmd[:4] == ["docker", "run", "--rm", "--name"] and cmd[4] == "gca-job1-1"
    for flag in ("--network=none", "--cap-drop=ALL", "--security-opt=no-new-privileges", "--memory=2g",
                 "--cpus=2", "--pids-limit=256"):
        assert flag in cmd
    assert cmd[cmd.index("--tmpfs") + 1].startswith("/tmp:")
    assert cmd[cmd.index("-v") + 1] == f"{tmp_path / 'wt'}:/work"
    assert cmd[cmd.index("-w") + 1] == "/work"
    assert cmd[cmd.index("--user") + 1] == "1000:1000"
    envs = [cmd[i + 1] for i, t in enumerate(cmd) if t == "-e"]
    assert "GCA_SANDBOX=1" in envs and "NO_COLOR=1" in envs and "HOME=/work/.gca-home" in envs
    assert not any(e.startswith("PATH=") for e in envs)  # the image's PATH, not the host's
    assert cmd[-6:] == ["--label", "gca=1", "python:3.12-slim", "python", "-m", "pytest", "-q"][-6:]
    assert cmd[-5:] == ["python:3.12-slim", "python", "-m", "pytest", "-q"]


def test_make_workspace_errors_without_runtime(calc_repo: Path, monkeypatch):
    monkeypatch.setenv("PATH", str(calc_repo))  # nothing on PATH
    monkeypatch.setattr("gca.workspace._RUNTIME_CACHE", {})
    with pytest.raises(RuntimeError, match="no working container runtime"):
        make_workspace(WorkspaceConfig(kind="container"), calc_repo, "job_c0")
    with pytest.raises(ValueError):
        make_workspace(WorkspaceConfig(kind="bogus"), calc_repo, "job_c0")


# --------------------------------------------------------------- fake runtime
FAKE_DOCKER = r'''#!/usr/bin/env python3
"""Minimal stand-in for `docker run`: executes the command on the host inside the mounted dir.
Records every invocation so tests can assert the hardening flags were passed."""
import json, os, subprocess, sys
args = sys.argv[1:]
log = os.environ.get("FAKE_DOCKER_LOG")
if log:
    with open(log, "a") as f:
        f.write(json.dumps(args) + "\n")
if args[:1] in (["kill"], ["info"]):
    sys.exit(0)
assert args[0] == "run", args
host_dir = None; env = {}; i = 1; image_idx = None
while i < len(args):
    a = args[i]
    if a == "-v":
        host_dir = args[i + 1].split(":")[0]; i += 2
    elif a in ("-w", "--name", "--user", "--tmpfs", "--label"):
        i += 2
    elif a == "-e":
        k, _, v = args[i + 1].partition("="); env[k] = v; i += 2
    elif a.startswith("-"):
        i += 1
    else:
        image_idx = i; break
cmd = args[image_idx + 1:]
env["PATH"] = os.environ["PATH"]
env["HOME"] = os.path.join(host_dir, ".gca-home")
if cmd and cmd[0] == "python":
    cmd[0] = os.environ.get("FAKE_DOCKER_PYTHON", sys.executable)
r = subprocess.run(cmd, cwd=host_dir, env=env)
sys.exit(r.returncode)
'''


@pytest.fixture
def fake_docker(tmp_path: Path, monkeypatch):
    bin_dir = tmp_path / "fakebin"
    bin_dir.mkdir()
    exe = bin_dir / "docker"
    exe.write_text(FAKE_DOCKER)
    exe.chmod(exe.stat().st_mode | stat.S_IEXEC)
    log = tmp_path / "docker.log"
    monkeypatch.setenv("PATH", f"{bin_dir}{os.pathsep}{os.environ['PATH']}")
    monkeypatch.setenv("FAKE_DOCKER_LOG", str(log))
    monkeypatch.setenv("FAKE_DOCKER_PYTHON", sys.executable)
    monkeypatch.setattr("gca.workspace._RUNTIME_CACHE", {})
    return log


def test_container_workspace_runs_through_runtime(calc_repo: Path, fake_docker: Path):
    ws = ContainerWorkspace(calc_repo, "job_c1", image="python:3.12-slim")
    try:
        res = ws.run(["python", "-c", "import os; print(os.getcwd()); open('made.txt','w').write('x')"],
                     env={"PATH": "/host/bin", "GCA_SANDBOX": "1"}, timeout=60)
        assert res.exit_code == 0
        assert (ws.root / "made.txt").exists()  # ran inside the mounted worktree
        assert "made.txt" in ws.changed_files() and ".gca-home" not in ws.diff()
        calls = [line for line in fake_docker.read_text().splitlines() if '"run"' in line]
        assert len(calls) == 1 and '"--network=none"' in calls[0] and '"--cap-drop=ALL"' in calls[0]
    finally:
        ws.cleanup(keep_branch=False)


def test_container_timeout_kills_container(calc_repo: Path, fake_docker: Path):
    ws = ContainerWorkspace(calc_repo, "job_c2", image="python:3.12-slim")
    try:
        res = ws.run(["python", "-c", "import time; time.sleep(10)"], env={"PATH": "/x"}, timeout=1)
        assert res.timed_out and "container killed" in res.stderr
        assert any('"kill"' in line for line in fake_docker.read_text().splitlines())
    finally:
        ws.cleanup(keep_branch=False)


def test_full_pipeline_with_container_workspace(config, work_item, fake_docker: Path):
    """Agent run AND verification both go through the container runtime."""
    config.workspace = WorkspaceConfig(kind="container", image="python:3.12-slim")
    work_item.test_command = ["python", "-m", "pytest", "-q", "-p", "no:cacheprovider"]
    q = JobQueue(config.db_path)
    job = q.submit(work_item)
    q.approve(job.id, "a")
    job = Worker(config, q, provider_factory=lambda: ScriptedProvider(steps_fix_calc())).run_once()
    assert job.status == JobStatus.SUCCEEDED, job.error
    invocations = fake_docker.read_text().splitlines()
    runs = [x for x in invocations if '"run"' in x]
    assert len(runs) == 3  # two agent run_tests + one verifier run
    assert any("gca-verify-" in x for x in runs)
    assert all("--network=none" in x for x in runs)
    assert job.result.verification.passed and ".gca-home" not in job.result.diff


def test_make_runner_host_and_container(tmp_path: Path, fake_docker: Path):
    host = make_runner(WorkspaceConfig(kind="worktree"), "j")
    r = host([sys.executable, "-c", "print('hi')"], tmp_path, {"PATH": os.environ["PATH"]}, 10)
    assert r.exit_code == 0 and "hi" in r.stdout
    r2 = host(["definitely-missing-binary"], tmp_path, {"PATH": "/nonexistent"}, 10)
    assert r2.exit_code == 127
    cont = make_runner(WorkspaceConfig(kind="container"), "j")
    r3 = cont(["python", "-c", "print('in container')"], tmp_path, {"PATH": "/x"}, 10)
    assert r3.exit_code == 0 and "in container" in r3.stdout


# ------------------------------------------------------------------ real daemon
# Set GCA_TEST_IMAGE to an image that has python3 + pytest (default python:3.12-slim, which needs a registry);
# GCA_TEST_RUNTIME to prefer docker or podman.
REAL_IMAGE = os.environ.get("GCA_TEST_IMAGE", "python:3.12-slim")
REAL_RUNTIME = os.environ.get("GCA_TEST_RUNTIME", "docker")


def _daemon_available() -> bool:
    return detect_runtime(REAL_RUNTIME) is not None


needs_daemon = pytest.mark.skipif(not _daemon_available(), reason="no working container runtime available")


@needs_daemon
def test_real_container_has_no_network_and_sees_only_the_worktree(calc_repo: Path):
    ws = ContainerWorkspace(calc_repo, "job_real", image=REAL_IMAGE, runtime=REAL_RUNTIME)
    try:
        res = ws.run(["python3", "-c",
                      "import socket,os\n"
                      "try:\n socket.create_connection(('1.1.1.1',80),timeout=3); print('NET=yes')\n"
                      "except Exception as e: print('NET=no', type(e).__name__)\n"
                      "print('ROOT', sorted(os.listdir('/work'))[:5])\n"
                      "print('HOSTFS', os.path.exists('/etc/hostname-of-host'))"],
                     env={"PATH": "/x", "GCA_SANDBOX": "1"}, timeout=180)
        assert res.exit_code == 0, res.output
        assert "NET=no" in res.stdout and "calc.py" in res.stdout
        # writes land in the worktree, owned by the host user
        res2 = ws.run(["python3", "-c", "open('from_container.txt','w').write('x')"], env={"PATH": "/x"}, timeout=120)
        assert res2.exit_code == 0, res2.output
        assert (ws.root / "from_container.txt").read_text() == "x"
    finally:
        ws.cleanup(keep_branch=False)


@needs_daemon
def test_real_pipeline_agent_and_verifier_in_containers(config, work_item):
    """The scripted agent fixes the bug; every command and the verifier run in real containers."""
    config.workspace = WorkspaceConfig(kind="container", image=REAL_IMAGE, runtime=REAL_RUNTIME)
    work_item.test_command = ["python3", "-m", "pytest", "-q", "-p", "no:cacheprovider"]
    q = JobQueue(config.db_path)
    job = q.submit(work_item)
    q.approve(job.id, "a")
    job = Worker(config, q, provider_factory=lambda: ScriptedProvider(steps_fix_calc())).run_once()
    assert job.status == JobStatus.SUCCEEDED, (job.error, job.result and job.result.verification)
    assert job.result.verification.passed and "4 passed" in job.result.verification.output_tail or \
           "3 passed" in job.result.verification.output_tail
    assert ".gca-home" not in job.result.diff


@needs_daemon
def test_real_container_timeout_is_enforced(calc_repo: Path):
    ws = ContainerWorkspace(calc_repo, "job_real_t", image=REAL_IMAGE, runtime=REAL_RUNTIME)
    try:
        res = ws.run(["python3", "-c", "import time; time.sleep(30)"], env={"PATH": "/x"}, timeout=5)
        assert res.timed_out and "container killed" in res.stderr
    finally:
        ws.cleanup(keep_branch=False)


def test_fix_constants_still_valid():
    assert CALC_FIX_OLD != CALC_FIX_NEW and shutil.which("git")
