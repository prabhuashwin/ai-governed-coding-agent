"""Work package 3: one instance per tenant — repository allow-list, publishing identity, TLS placement."""

from __future__ import annotations

import json
import os
from pathlib import Path

import httpx
import pytest

from gca import webhooks
from gca.cli import main as cli_main
from gca.config import ApprovalConfig, PublishConfig, WebhookConfig
from gca.inbound import CommandHandler, make_server
from gca.policy import PolicyViolation, check_repo_allowed
from gca.queue import JobQueue
from gca.sources.servicenow import sync_once
from gca.webhooks import signed_headers


def test_check_repo_allowed(tmp_path):
    root = tmp_path / "tenant-a"
    (root / "repo").mkdir(parents=True)
    other = tmp_path / "tenant-b" / "repo"
    other.mkdir(parents=True)
    assert check_repo_allowed([], str(other)) == str(other.resolve())  # empty = any (single-developer use)
    assert check_repo_allowed([str(root)], str(root / "repo")) == str((root / "repo").resolve())
    assert check_repo_allowed([str(root)], str(root)) == str(root.resolve())
    with pytest.raises(PolicyViolation, match="allowed_repos"):
        check_repo_allowed([str(root)], str(other))
    with pytest.raises(PolicyViolation):  # a sibling whose name merely starts with the root
        check_repo_allowed([str(root)], str(tmp_path / "tenant-a-evil"))
    with pytest.raises(PolicyViolation):  # traversal out of the root
        check_repo_allowed([str(root)], str(root / "repo" / ".." / ".." / "tenant-b" / "repo"))
    if os.name != "nt":
        link = root / "link"
        link.symlink_to(other, target_is_directory=True)
        with pytest.raises(PolicyViolation):  # a symlink inside the root pointing outside it
            check_repo_allowed([str(root)], str(link))


@pytest.fixture
def tenant_cfg(config, calc_repo, tmp_path, monkeypatch):
    monkeypatch.setenv("GCA_WEBHOOK_SECRET", "out")
    monkeypatch.setenv("GCA_INBOUND_SECRET", "in")
    config.webhooks = WebhookConfig(enabled=True, outbound_urls=[])
    config.approval = ApprovalConfig(mode="webhook")
    config.policy.allowed_repos = [str(Path(calc_repo).parent)]
    webhooks.set_default_transport(httpx.MockTransport(lambda r: httpx.Response(204)))
    yield config
    webhooks.set_default_transport(None)
    webhooks.uninstall()


def _signed(method, path, body, delivery_id):
    raw = json.dumps(body).encode()
    return signed_headers("in", method, path, raw, delivery_id=delivery_id), raw


def test_inbound_submit_outside_allowed_repos_is_403(tenant_cfg, work_item, tmp_path):
    q = JobQueue(tenant_cfg.db_path)
    webhooks.install(tenant_cfg)
    h = CommandHandler(tenant_cfg, q)
    foreign = tmp_path.parent / "elsewhere"
    foreign.mkdir(exist_ok=True)
    body = {"title": "t", "description": "d", "acceptance_criteria": [], "repo_path": str(foreign),
            "test_command": ["pytest"], "requester": "dev"}
    hd, rw = _signed("POST", "/v1/work-items", body, "r-1")
    s, r = h.handle("POST", "/v1/work-items", hd, rw)
    assert s == 403 and "allowed_repos" in r["error"] and q.list() == []
    body["repo_path"] = work_item.repo_path
    hd, rw = _signed("POST", "/v1/work-items", body, "r-2")
    assert h.handle("POST", "/v1/work-items", hd, rw)[0] == 201


def test_cli_submit_outside_allowed_repos_exits_2(tmp_path, calc_repo, capsys):
    toml = tmp_path / "gca.toml"
    toml.write_text(f'data_dir = "{(tmp_path / "d").as_posix()}"\n[policy]\nallowed_repos = ["{Path(calc_repo).as_posix()}"]\n')
    assert cli_main(["-c", str(toml), "submit", "--title", "t", "--description", "d", "--repo", str(tmp_path)]) == 2
    assert "allowed_repos" in capsys.readouterr().err
    assert cli_main(["-c", str(toml), "submit", "--title", "t", "--description", "d", "--repo", str(calc_repo)]) == 0


def test_servicenow_record_outside_allowed_repos_is_an_error(config, tmp_path, monkeypatch):
    from unit.test_servicenow import REC, FakeTableAPI, make_source
    monkeypatch.setenv("SN_USER", "u")
    monkeypatch.setenv("SN_PASSWORD", "p")
    config.policy.allowed_repos = [str(tmp_path / "nowhere")]
    api = FakeTableAPI([{**REC, "sys_id": "x1", "number": "WI0009001"}])
    q = JobQueue(config.db_path)
    rep = sync_once(config, q, make_source(api))
    assert len(rep["errors"]) == 1 and "allowed_repos" in rep["errors"][0] and q.list() == []


def test_publish_config_drives_cli_and_inbound(tenant_cfg, work_item, calc_repo, tmp_path, monkeypatch, capsys):
    """The tenant's publishing identity lives in [publish]: remote, base, draft, token variable, API base.
    Both `gca publish` and the inbound publish command pass it through; a request may narrow remote/base/draft
    but cannot choose another token or API."""
    import gca.inbound as inbound_mod
    from gca.publish import PublishResult
    tenant_cfg.publish = PublishConfig(remote="tenant", base="develop", draft=False, token_env="TENANT_A_GH_TOKEN",
                                       api_base="https://ghe.tenant-a.example/api/v3")
    calls: list[dict] = []

    def fake_publish(cfg, queue, job_id, **kw):
        calls.append(kw)
        return PublishResult(job_id=job_id, branch="gca/x", pushed_to="tenant", pr_url=None, note="")

    monkeypatch.setattr(inbound_mod, "publish_job", fake_publish)
    monkeypatch.setattr("gca.publish.publish_job", fake_publish)
    webhooks.install(tenant_cfg)
    q = JobQueue(tenant_cfg.db_path)
    job = q.submit(work_item)
    q.approve(job.id, "lead")
    from gca.models import JobStatus, RunResult
    q.transition(job.id, JobStatus.RUNNING)
    q.transition(job.id, JobStatus.VERIFYING)
    q.complete(job.id, JobStatus.SUCCEEDED, RunResult(job_id=job.id, branch=f"gca/{job.id}"))
    # inbound: defaults from [publish], request may narrow remote/base/draft only
    h = CommandHandler(tenant_cfg, q)
    path = f"/v1/jobs/{job.id}/publish"
    hd, rw = _signed("POST", path, {"actor": "alice"}, "p-1")
    assert h.handle("POST", path, hd, rw)[0] == 200
    assert calls[-1] == {"remote": "tenant", "base": "develop", "draft": False, "create_pr": True,
                         "token_env": "TENANT_A_GH_TOKEN", "api_base": "https://ghe.tenant-a.example/api/v3",
                         "actor": "alice"}
    hd, rw = _signed("POST", path, {"actor": "alice", "remote": "mirror", "draft": True, "token_env": "MINE",
                                    "api_base": "https://evil.example"}, "p-2")
    assert h.handle("POST", path, hd, rw)[0] == 200
    assert calls[-1]["remote"] == "mirror" and calls[-1]["draft"] is True
    assert calls[-1]["token_env"] == "TENANT_A_GH_TOKEN" and calls[-1]["api_base"].startswith("https://ghe.tenant-a")
    # CLI: same defaults
    toml = tmp_path / "gca.toml"
    toml.write_text(f'data_dir = "{Path(tenant_cfg.data_dir).as_posix()}"\n[publish]\nremote = "tenant"\n'
                    'base = "develop"\ndraft = false\ntoken_env = "TENANT_A_GH_TOKEN"\n'
                    'api_base = "https://ghe.tenant-a.example/api/v3"\n')
    q.close()
    assert cli_main(["-c", str(toml), "publish", job.id]) == 0
    capsys.readouterr()
    assert calls[-1]["remote"] == "tenant" and calls[-1]["base"] == "develop" and calls[-1]["draft"] is False
    assert calls[-1]["token_env"] == "TENANT_A_GH_TOKEN"


def test_plain_http_bind_off_loopback_needs_tls_proxy(tenant_cfg):
    q = JobQueue(tenant_cfg.db_path)
    try:
        with pytest.raises(ValueError, match="behind_tls_proxy"):
            make_server(tenant_cfg, q, "0.0.0.0", 0)
        tenant_cfg.webhooks.behind_tls_proxy = True
        srv = make_server(tenant_cfg, q, "0.0.0.0", 0)
        srv.server_close()
        srv = make_server(tenant_cfg, q, "127.0.0.1", 0)  # loopback never needs the flag
        srv.server_close()
    finally:
        q.close()
