"""Tests for paths the coverage report showed untested: the worker's crash handling, the provider registry,
and the CLI's webhook, ServiceNow, publish and eval commands."""

from __future__ import annotations

import json
import sys
from pathlib import Path

import httpx
import pytest
from conftest import git, steps_fix_calc

from gca import webhooks
from gca.audit import AuditLog, verify_chain
from gca.cli import main as cli_main
from gca.config import ProviderConfig
from gca.models import JobStatus
from gca.providers import build_provider
from gca.providers.base import ProviderError
from gca.providers.testing import RecordingProvider, ReplayProvider, ScriptedProvider
from gca.queue import JobQueue
from gca.worker import Worker


# ------------------------------------------------------------------ worker crash handling
def test_worker_turns_an_internal_crash_into_a_failed_job(config, work_item):
    q = JobQueue(config.db_path)
    job = q.submit(work_item)
    q.approve(job.id, "lead")

    def exploding_provider():
        raise RuntimeError("provider construction blew up")

    done = Worker(config, q, provider_factory=exploding_provider).run_once()
    assert done.status == JobStatus.FAILED and "internal_error" in done.error
    events = [e.event for e in AuditLog(config.audit_dir, job.id).events()]
    assert "internal_error" in events and events[-1] == "finished"
    assert verify_chain(AuditLog(config.audit_dir, job.id).path)[0]
    # the workspace was cleaned up even though the job crashed
    assert "gca/" + job.id in git(Path(work_item.repo_path), "branch", "--list")
    assert len(git(Path(work_item.repo_path), "worktree", "list").strip().splitlines()) == 1


def test_worker_survives_a_bad_repo_path(config, work_item, tmp_path):
    work_item.repo_path = str(tmp_path / "not-a-repo")
    q = JobQueue(config.db_path)
    job = q.submit(work_item)
    q.approve(job.id, "lead")
    done = Worker(config, q, provider_factory=lambda: ScriptedProvider(steps_fix_calc())).run_once()
    assert done.status == JobStatus.FAILED and "not a git repository" in done.error


def test_run_forever_processes_then_stops(config, work_item):
    q = JobQueue(config.db_path)
    for _ in range(2):
        q.approve(q.submit(work_item).id, "lead")
    w = Worker(config, q, provider_factory=lambda: ScriptedProvider(steps_fix_calc()))
    assert w.run_forever(poll_seconds=0.01, max_jobs=2) == 2
    assert [j.status for j in q.list()] == [JobStatus.SUCCEEDED, JobStatus.SUCCEEDED]


# ------------------------------------------------------------------ provider registry
def test_build_provider_all_kinds(tmp_path, monkeypatch):
    monkeypatch.setenv("K", "x")
    assert build_provider(ProviderConfig(kind="anthropic", api_key_env="K")).name == "anthropic"
    for alias in ("openai_compatible", "openai", "azure", "ollama", "vllm", "openrouter", "groq"):
        assert build_provider(ProviderConfig(kind=alias, api_key_env="K")).name == "openai_compatible"
    script = tmp_path / "s.json"
    script.write_text(json.dumps({"steps": [{"text": "hi"}]}))
    assert isinstance(build_provider(ProviderConfig(kind="scripted", script_path=str(script))), ScriptedProvider)
    cassette = tmp_path / "c.json"
    cassette.write_text(json.dumps({"model": "m", "exchanges": []}))
    assert isinstance(build_provider(ProviderConfig(kind="replay", cassette_path=str(cassette))), ReplayProvider)
    rec = build_provider(ProviderConfig(kind="anthropic", api_key_env="K", record=True, cassette_path=str(cassette)))
    assert isinstance(rec, RecordingProvider)
    for bad in (ProviderConfig(kind="scripted"), ProviderConfig(kind="replay"), ProviderConfig(kind="nope"),
                ProviderConfig(kind="anthropic", api_key_env="K", record=True)):
        with pytest.raises(ProviderError):
            build_provider(bad)


# ------------------------------------------------------------------ CLI commands
def _toml(tmp_path: Path, extra: str = "") -> Path:
    p = tmp_path / "gca.toml"
    py = Path(sys.executable).name
    p.write_text(f'data_dir = "{(tmp_path / "d").as_posix()}"\n[policy]\nallowed_commands = ["python", "{py}"]\n'
                 + extra)
    return p


def test_cli_webhook_status_and_deliver(tmp_path, work_item, monkeypatch, capsys):
    monkeypatch.setenv("GCA_WEBHOOK_SECRET", "s")
    got: list[str] = []

    def receiver(req: httpx.Request) -> httpx.Response:
        got.append(json.loads(req.content)["type"])
        return httpx.Response(204) if len(got) > 1 else httpx.Response(503)

    webhooks.set_default_transport(httpx.MockTransport(receiver))
    try:
        cfg = _toml(tmp_path, '[webhooks]\nenabled = true\noutbound_urls = ["https://koru.example/h"]\n')
        assert cli_main(["-c", str(cfg), "submit", "--title", "t", "--description", "d",
                         "--repo", work_item.repo_path]) == 0
        capsys.readouterr()
        cli_main(["-c", str(cfg), "webhook-status"])
        out = capsys.readouterr().out
        assert "job.submitted" in out and '"pending": 1' in out  # first attempt got a 503
        from gca.webhooks import Outbox
        ob = Outbox(tmp_path / "d" / "jobs.sqlite3")
        ob._conn.execute("UPDATE webhook_outbox SET next_attempt_at='2000-01-01'")
        assert cli_main(["-c", str(cfg), "webhook-deliver"]) == 0
        assert '"delivered": 1' in capsys.readouterr().out
        cli_main(["-c", str(cfg), "webhook-status", "--status", "delivered"])
        assert '"delivered": 1' in capsys.readouterr().out
        assert cli_main(["-c", str(cfg), "webhook-deliver", "--requeue-dead"]) == 0
        assert "requeued 0" in capsys.readouterr().out
    finally:
        webhooks.set_default_transport(None)
        webhooks.uninstall()


def test_cli_webhook_serve_refuses_without_secret(tmp_path, monkeypatch, capsys):
    monkeypatch.delenv("GCA_INBOUND_SECRET", raising=False)
    assert cli_main(["-c", str(_toml(tmp_path)), "webhook-serve"]) == 2
    assert "GCA_INBOUND_SECRET" in capsys.readouterr().err


def test_cli_servicenow_sync_once(tmp_path, work_item, monkeypatch, capsys):
    monkeypatch.setenv("SN_USER", "u")
    monkeypatch.setenv("SN_PASSWORD", "p")
    rec = {"sys_id": "s1", "number": "WI1", "state": "approved", "short_description": "Fix mean()",
           "description": "d", "acceptance_criteria": "a", "repo_path": work_item.repo_path,
           "approver": {"display_value": "lead"}, "gca_job_id": ""}

    def api(req: httpx.Request) -> httpx.Response:
        if req.method == "GET":
            q = req.url.params.get("sysparm_query", "")
            return httpx.Response(200, json={"result": [rec] if "ISEMPTY" in q else []})
        return httpx.Response(200, json={"result": rec})

    real_client = httpx.Client
    monkeypatch.setattr("gca.sources.servicenow.httpx.Client",
                        lambda **kw: real_client(transport=httpx.MockTransport(api), **{k: v for k, v in kw.items()
                                                                                      if k != "transport"}))
    cfg = _toml(tmp_path, '[servicenow]\ninstance_url = "https://dev1.service-now.com"\n')
    assert cli_main(["-c", str(cfg), "servicenow-sync", "--once"]) == 0
    out = capsys.readouterr().out
    assert out.startswith("queued: WI1->job_")


def test_cli_publish_refuses_failed_job(tmp_path, work_item, capsys):
    cfg = _toml(tmp_path)
    cli_main(["-c", str(cfg), "submit", "--title", "t", "--description", "d", "--repo", work_item.repo_path])
    jid = capsys.readouterr().out.strip()
    with pytest.raises(ValueError, match="no branch"):
        cli_main(["-c", str(cfg), "publish", jid])


def test_cli_eval_with_scripted_provider(tmp_path, monkeypatch, capsys):
    tasks = Path(__file__).resolve().parents[2] / "evals" / "tasks"
    steps = [{"tool_calls": [{"name": "edit_file", "arguments": {
        "path": "stats.py", "old": "return sum(values) / (len(values) - 1)", "new": "return sum(values) / len(values)"}}]},
        {"tool_calls": [{"name": "finish", "arguments": {"summary": "fixed", "success": True}}]}]
    script = tmp_path / "s.json"
    script.write_text(json.dumps({"steps": steps}))
    cfg = _toml(tmp_path, f'[provider]\nkind = "scripted"\nscript_path = "{script.as_posix()}"\n')
    # task.json asks for `python -m pytest`; make sure that resolves to this interpreter
    monkeypatch.setenv("PATH", str(Path(sys.executable).parent) + ":" + __import__("os").environ["PATH"])
    assert cli_main(["-c", str(cfg), "eval", "--tasks-dir", str(tasks), "--out", str(tmp_path / "res"),
                     "--only", "01-off-by-one"]) == 0
    out = capsys.readouterr().out
    assert "1/1 passed" in out
    assert list((tmp_path / "res").glob("*.md")) and list((tmp_path / "res").glob("*.json"))


def test_cli_status_and_report_without_result(tmp_path, work_item, capsys):
    cfg = _toml(tmp_path)
    cli_main(["-c", str(cfg), "submit", "--title", "t", "--description", "d", "--repo", work_item.repo_path])
    jid = capsys.readouterr().out.strip()
    assert cli_main(["-c", str(cfg), "report", jid]) == 1
    assert "no result yet" in capsys.readouterr().out
    assert cli_main(["-c", str(cfg), "audit", "job_missing"]) == 1
    assert "no audit log" in capsys.readouterr().out
    cli_main(["-c", str(cfg), "status", jid])
    assert json.loads(capsys.readouterr().out)["status"] == "submitted"
