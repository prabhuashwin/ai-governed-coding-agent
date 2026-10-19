"""Publish a finished job: push its branch and open a pull request for human review.

This is the only place gca touches a remote, and it is an explicit human-invoked step
(`gca publish <job>`), never part of the worker's automatic pipeline.
"""

from __future__ import annotations

import os
import re
import subprocess
from dataclasses import dataclass
from urllib.parse import urlsplit

import httpx

from .audit import AuditLog
from .config import Config
from .models import JobStatus
from .queue import JobQueue


@dataclass
class PublishResult:
    job_id: str
    branch: str
    pushed_to: str
    pr_url: str | None = None
    note: str = ""


def api_host(api_base: str) -> str | None:
    """The git host an API base serves: api.github.com -> github.com; https://ghe.example/api/v3 -> ghe.example."""
    host = (urlsplit(api_base).hostname or "").lower()
    if not host:
        return None
    return "github.com" if host in ("api.github.com", "github.com") else host


def parse_remote(remote_url: str) -> tuple[str | None, str | None]:
    """(host, 'owner/repo') of a git remote: https://[user@]host[:port]/owner/repo(.git), ssh://git@host[:port]/owner/repo,
    or scp-like git@host:owner/repo(.git). The host is the real hostname (port and credentials stripped), never a
    substring found elsewhere in the URL, so a path like https://evil.example/github.com/o/r is evil.example."""
    s = remote_url.strip()
    if "://" in s:
        try:
            u = urlsplit(s)
        except ValueError:
            return None, None
        host, path = (u.hostname or "").lower(), u.path
    else:
        m = re.match(r"^(?:[^@/:]+@)?([^@:/]+):(?!//)(.+)$", s)  # scp-like; a leading drive letter is not a host
        if not m or len(m.group(1)) == 1:
            return None, None
        host, path = m.group(1).lower(), m.group(2)
    if not host:
        return None, None
    path = path.strip("/")
    if path.endswith(".git"):
        path = path[:-4]
    parts = [p for p in path.split("/") if p]
    if len(parts) != 2:
        return host, None
    return host, f"{parts[0]}/{parts[1]}"


def github_slug(remote_url: str, host: str | None = None) -> str | None:
    """'owner/repo' when the remote's hostname is github.com or, for GitHub Enterprise, the host the configured
    `api_base` serves (otherwise an Enterprise remote would be taken for "not GitHub" and the pull request silently
    skipped). Hostnames are compared exactly: ports and credentials are ignored, substrings never match."""
    hosts = {"github.com"} | ({host.lower()} if host else set())
    h, slug = parse_remote(remote_url)
    return slug if h in hosts else None


def _git(repo: str, *args: str) -> str:
    cp = subprocess.run(["git", *args], cwd=repo, capture_output=True, text=True)
    if cp.returncode != 0:
        raise RuntimeError(f"git {' '.join(args)} failed: {cp.stderr.strip()[:500]}")
    return cp.stdout.strip()


def publish_job(cfg: Config, queue: JobQueue, job_id: str, *, remote: str = "origin", base: str | None = None,
                draft: bool = True, create_pr: bool = True, token_env: str = "GITHUB_TOKEN",
                api_base: str = "https://api.github.com", transport: httpx.BaseTransport | None = None,
                allow_failed: bool = False, actor: str = "publisher") -> PublishResult:
    job = queue.get(job_id)
    if not job.result or not job.result.branch:
        raise ValueError(f"{job_id} has no branch to publish (status {job.status.value})")
    if job.status != JobStatus.SUCCEEDED and not allow_failed:
        raise ValueError(f"{job_id} is {job.status.value}; pass --allow-failed to publish anyway for inspection")
    repo = job.work_item.repo_path
    branch = job.result.branch
    audit = AuditLog(cfg.audit_dir, job.id, redactions=cfg.policy.canary_strings)

    remote_url = _git(repo, "remote", "get-url", remote)
    verified = job.result.verification.commit if job.result.verification else None
    if verified:
        # The report describes exactly one commit. If the branch has moved since, the evidence no longer applies.
        head = _git(repo, "rev-parse", "--verify", f"{branch}^{{commit}}")
        if head != verified and not allow_failed:
            raise ValueError(f"{branch} is at {head[:12]} but the verified commit is {verified[:12]}; the branch "
                             f"changed after verification. Re-run the job, or pass --allow-failed to push it unverified")
        _git(repo, "push", "--force-with-lease", remote, f"{verified}:refs/heads/{branch}")
    elif allow_failed:  # explicit override only: a job from before verification recorded its commit
        _git(repo, "push", "--force-with-lease", remote, f"{branch}:{branch}")
    else:
        raise ValueError(f"{job_id} has no verified commit on record (it predates commit pinning); re-run the job, "
                         f"or pass --allow-failed to push the branch head unverified")
    audit.record(actor, "branch_pushed", remote=remote_url, branch=branch, commit=verified)
    result = PublishResult(job_id=job.id, branch=branch, pushed_to=remote_url)

    if not create_pr:
        result.note = "branch pushed; PR creation skipped"
        return result
    slug = github_slug(remote_url, api_host(api_base))
    token = os.environ.get(token_env) or os.environ.get("GH_TOKEN")
    if not slug:
        result.note = "branch pushed; remote is not GitHub, open the merge request manually"
        return result
    if not token:
        result.note = f"branch pushed; set {token_env} to open the pull request automatically"
        return result

    if base is None:
        try:
            base = _git(repo, "symbolic-ref", "--short", f"refs/remotes/{remote}/HEAD").split("/", 1)[1]
        except RuntimeError:
            base = "main"
    body = (job.result.report_markdown or "") + (
        "\n\n---\n_Opened by gca. This change was produced by a coding agent under policy and independently "
        "verified; it still requires human review before merge._")
    title = f"[gca] {job.work_item.title}"[:250]
    try:
        with httpx.Client(timeout=30, transport=transport,
                          headers={"Authorization": f"Bearer {token}", "Accept": "application/vnd.github+json",
                                   "X-GitHub-Api-Version": "2022-11-28"}) as c:
            r = c.post(f"{api_base}/repos/{slug}/pulls",
                       json={"title": title, "head": branch, "base": base, "body": body[:60000], "draft": draft})
            if r.status_code == 422 and "already exists" in r.text:
                existing = c.get(f"{api_base}/repos/{slug}/pulls",
                                 params={"head": f"{slug.split('/')[0]}:{branch}", "state": "open"})
                if existing.status_code >= 400:
                    raise RuntimeError(f"GitHub {existing.status_code} listing pull requests: {existing.text[:300]}")
                items = existing.json()
                result.pr_url = items[0]["html_url"] if items else None
                result.note = "pull request already existed"
            elif r.status_code >= 400:
                raise RuntimeError(f"GitHub {r.status_code} creating the pull request: {r.text[:300]}")
            else:
                result.pr_url = r.json().get("html_url")
    except httpx.HTTPError as e:  # transport failure (DNS, TLS, timeout): the branch is pushed, the PR is not
        raise RuntimeError(f"GitHub unreachable ({type(e).__name__}: {e}); branch {branch} was pushed") from e
    except ValueError as e:  # non-JSON body
        raise RuntimeError(f"GitHub returned an unreadable response: {e}") from e
    audit.record(actor, "pull_request_opened", url=result.pr_url, base=base, draft=draft)
    return result
