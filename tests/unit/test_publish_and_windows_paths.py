"""`gca publish` against a local bare remote + mocked GitHub API, and Windows-style path handling in policy."""

from __future__ import annotations

import json
from pathlib import Path

import httpx
import pytest
from conftest import git, steps_fix_calc

from gca.config import PolicyConfig
from gca.models import JobStatus
from gca.policy import PolicyEngine, PolicyViolation
from gca.providers.testing import ScriptedProvider
from gca.publish import github_slug, publish_job
from gca.queue import JobQueue
from gca.worker import Worker


# ------------------------------------------------------------------ publish
@pytest.mark.parametrize("url,slug", [
    ("https://github.com/prabhuashwin/ai-governed-coding-agent.git", "prabhuashwin/ai-governed-coding-agent"),
    ("https://github.com/prabhuashwin/ai-governed-coding-agent", "prabhuashwin/ai-governed-coding-agent"),
    ("git@github.com:org/repo.git", "org/repo"),
    ("ssh://git@github.com/org/repo", "org/repo"),
    ("https://gitlab.com/org/repo.git", None),
    ("/srv/git/bare.git", None),
])
def test_github_slug(url, slug):
    assert github_slug(url) == slug


@pytest.fixture
def succeeded_job(config, work_item, calc_repo, tmp_path):
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
    return q, job, bare


def test_publish_pushes_branch_and_opens_draft_pr(config, succeeded_job, monkeypatch):
    q, job, bare = succeeded_job
    monkeypatch.setenv("GITHUB_TOKEN", "ghp_test")
    seen = {}

    def api(req: httpx.Request) -> httpx.Response:
        seen["url"] = str(req.url)
        seen["auth"] = req.headers.get("authorization")
        seen["body"] = json.loads(req.content)
        return httpx.Response(201, json={"html_url": "https://github.com/o/r/pull/7", "number": 7})

    # make the bare remote look like GitHub for slug detection
    git(job.work_item.repo_path, "remote", "set-url", "origin", str(bare))
    monkeypatch.setattr("gca.publish.github_slug", lambda url: "o/r")
    res = publish_job(config, q, job.id, transport=httpx.MockTransport(api))
    assert res.pr_url == "https://github.com/o/r/pull/7"
    assert seen["url"].endswith("/repos/o/r/pulls") and seen["auth"] == "Bearer ghp_test"
    assert seen["body"]["head"] == job.result.branch and seen["body"]["base"] == "main" and seen["body"]["draft"] is True
    assert seen["body"]["title"].startswith("[gca] Fix mean()") and "Independent verification: **PASSED**" in seen["body"]["body"]
    # the branch really is on the remote
    assert job.result.branch in git(job.work_item.repo_path, "ls-remote", "--heads", "origin")
    from gca.audit import AuditLog
    events = [e.event for e in AuditLog(config.audit_dir, job.id).events()]
    assert events[-2:] == ["branch_pushed", "pull_request_opened"]


def test_publish_without_token_pushes_only(config, succeeded_job, monkeypatch):
    q, job, bare = succeeded_job
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)
    monkeypatch.delenv("GH_TOKEN", raising=False)
    monkeypatch.setattr("gca.publish.github_slug", lambda url: "o/r")
    res = publish_job(config, q, job.id)
    assert res.pr_url is None and "GITHUB_TOKEN" in res.note
    assert job.result.branch in git(job.work_item.repo_path, "ls-remote", "--heads", "origin")


def test_publish_refuses_failed_jobs_unless_forced(config, work_item, calc_repo, tmp_path):
    bare = tmp_path / "remote.git"
    git(tmp_path, "init", "-q", "--bare", str(bare))
    git(calc_repo, "remote", "add", "origin", str(bare))
    q = JobQueue(config.db_path)
    job = q.submit(work_item)
    q.approve(job.id, "lead")
    steps = [{"tool_calls": [{"name": "finish", "arguments": {"summary": "gave up", "success": False}}]}]
    job = Worker(config, q, provider_factory=lambda: ScriptedProvider(steps)).run_once()
    assert job.status == JobStatus.FAILED
    with pytest.raises(ValueError, match="allow-failed"):
        publish_job(config, q, job.id)
    res = publish_job(config, q, job.id, allow_failed=True, create_pr=False)
    assert res.pr_url is None and "skipped" in res.note


def test_publish_existing_pr_is_reported(config, succeeded_job, monkeypatch):
    q, job, bare = succeeded_job
    monkeypatch.setenv("GITHUB_TOKEN", "t")
    monkeypatch.setattr("gca.publish.github_slug", lambda url: "o/r")

    def api(req: httpx.Request) -> httpx.Response:
        if req.method == "POST":
            return httpx.Response(422, json={"message": "Validation Failed", "errors": [{"message": "A pull request already exists for o:gca/x."}]})
        return httpx.Response(200, json=[{"html_url": "https://github.com/o/r/pull/3"}])

    res = publish_job(config, q, job.id, transport=httpx.MockTransport(api))
    assert res.pr_url == "https://github.com/o/r/pull/3" and "already existed" in res.note


# ------------------------------------------------------- windows-style paths
@pytest.fixture
def engine(tmp_path: Path) -> PolicyEngine:
    root = tmp_path / "ws"
    (root / "src").mkdir(parents=True)
    (root / "src" / "a.py").write_text("x = 1\n")
    return PolicyEngine(PolicyConfig(), root)


def test_backslash_relative_paths_are_normalised(engine: PolicyEngine):
    assert engine.resolve_path("src\\a.py") == engine.resolve_path("src/a.py")


@pytest.mark.parametrize("bad", [
    "C:\\Windows\\System32\\config\\SAM", "c:/users/x/.ssh/id_rsa", "D:secrets.txt",
    "\\\\server\\share\\file.txt", "//server/share/file.txt",
    "src\\..\\..\\outside.txt", "..\\outside.txt", "src\\..\\.env",
])
def test_windows_style_escapes_are_blocked(engine: PolicyEngine, bad: str):
    with pytest.raises(PolicyViolation):
        engine.resolve_path(bad)


@pytest.mark.parametrize("argv", [
    ["python", "C:\\evil.py"], ["python", "..\\..\\evil.py"], ["python", "\\\\server\\share\\x.py"],
    ["cmd", "/c", "dir"], ["powershell", "-c", "ls"], ["python", "-c", "x", "&&", "dir"],
])
def test_windows_style_command_args_are_blocked(engine: PolicyEngine, argv):
    with pytest.raises(PolicyViolation):
        engine.check_command(argv)


def test_exe_suffix_is_not_implicitly_allowed(engine: PolicyEngine):
    """python.exe must be allow-listed explicitly on Windows hosts; we do not strip suffixes."""
    with pytest.raises(PolicyViolation):
        engine.check_command(["python.exe", "-m", "pytest"])
    engine.cfg.allowed_commands.append("python.exe")
    assert engine.check_command(["python.exe", "-m", "pytest"])[0] == "python.exe"
