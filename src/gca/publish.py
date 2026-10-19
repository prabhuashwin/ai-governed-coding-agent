"""Publish a finished job: push its branch and open a pull request for human review.

This is the only place gca touches a remote, and it is an explicit human-invoked step
(`gca publish <job>`), never part of the worker's automatic pipeline.
"""

from __future__ import annotations

import os
import re
import subprocess
from dataclasses import dataclass

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


def github_slug(remote_url: str) -> str | None:
    """'owner/repo' from https://github.com/o/r(.git), git@github.com:o/r(.git), ssh://git@github.com/o/r."""
    m = re.search(r"github\.com[:/]+([^/]+)/([^/]+?)(?:\.git)?/?$", remote_url.strip())
    return f"{m.group(1)}/{m.group(2)}" if m else None


def _git(repo: str, *args: str) -> str:
    cp = subprocess.run(["git", *args], cwd=repo, capture_output=True, text=True)
    if cp.returncode != 0:
        raise RuntimeError(f"git {' '.join(args)} failed: {cp.stderr.strip()[:500]}")
    return cp.stdout.strip()


def publish_job(cfg: Config, queue: JobQueue, job_id: str, *, remote: str = "origin", base: str | None = None,
                draft: bool = True, create_pr: bool = True, token_env: str = "GITHUB_TOKEN",
                api_base: str = "https://api.github.com", transport: httpx.BaseTransport | None = None,
                allow_failed: bool = False) -> PublishResult:
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
    audit.record("publisher", "branch_pushed", remote=remote_url, branch=branch, commit=verified)
    result = PublishResult(job_id=job.id, branch=branch, pushed_to=remote_url)

    if not create_pr:
        result.note = "branch pushed; PR creation skipped"
        return result
    slug = github_slug(remote_url)
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
    with httpx.Client(timeout=30, transport=transport,
                      headers={"Authorization": f"Bearer {token}", "Accept": "application/vnd.github+json",
                               "X-GitHub-Api-Version": "2022-11-28"}) as c:
        r = c.post(f"{api_base}/repos/{slug}/pulls",
                   json={"title": title, "head": branch, "base": base, "body": body[:60000], "draft": draft})
        if r.status_code == 422 and "already exists" in r.text:
            existing = c.get(f"{api_base}/repos/{slug}/pulls", params={"head": f"{slug.split('/')[0]}:{branch}", "state": "open"})
            existing.raise_for_status()
            items = existing.json()
            result.pr_url = items[0]["html_url"] if items else None
            result.note = "pull request already existed"
        else:
            r.raise_for_status()
            result.pr_url = r.json().get("html_url")
    audit.record("publisher", "pull_request_opened", url=result.pr_url, base=base, draft=draft)
    return result
