"""Webhooks: signing, durable outbound delivery, inbound commands, approval mode and the kill switch."""

from __future__ import annotations

import json
import threading
import time
from pathlib import Path

import httpx
import pytest
from conftest import CALC_FIX_NEW, CALC_FIX_OLD, steps_fix_calc

from gca import webhooks
from gca.audit import AuditLog, verify_chain
from gca.cli import main as cli_main
from gca.config import ApprovalConfig, WebhookConfig
from gca.inbound import CommandHandler, make_server
from gca.models import JobStatus, StopReason
from gca.providers.testing import ScriptedProvider
from gca.queue import JobQueue
from gca.webhooks import Deliverer, Outbox, sign, signed_headers, verify_signature
from gca.worker import Worker

OUT_SECRET = "out-secret-123"
IN_SECRET = "in-secret-456"


class Receiver:
    """A fake platform endpoint: records events, verifies signatures, and can be told to fail."""

    def __init__(self, secret: str = OUT_SECRET):
        self.secret = secret
        self.events: list[dict] = []
        self.bad_sigs = 0
        self.fail_with: list[int] = []  # status codes to return, consumed in order

    def handler(self, req: httpx.Request) -> httpx.Response:
        if verify_signature(self.secret, dict(req.headers), req.content) is not None:
            self.bad_sigs += 1
            return httpx.Response(401)
        if self.fail_with:
            return httpx.Response(self.fail_with.pop(0))
        ev = json.loads(req.content)
        assert req.headers["x-gca-event"] == ev["type"] and req.headers["x-gca-delivery-id"] == ev["id"]
        self.events.append(ev)
        return httpx.Response(204)

    def types(self) -> list[str]:
        return [e["type"] for e in self.events]

    def of(self, t: str) -> list[dict]:
        return [e for e in self.events if e["type"] == t]


@pytest.fixture
def receiver(monkeypatch):
    monkeypatch.setenv("GCA_WEBHOOK_SECRET", OUT_SECRET)
    monkeypatch.setenv("GCA_INBOUND_SECRET", IN_SECRET)
    r = Receiver()
    webhooks.set_default_transport(httpx.MockTransport(r.handler))
    yield r
    webhooks.set_default_transport(None)
    webhooks.uninstall()


@pytest.fixture
def wh_config(config):
    config.webhooks = WebhookConfig(enabled=True, outbound_urls=["https://koru.example/hooks/gca"])
    return config


def signed(body: dict | None, *, secret: str = IN_SECRET, delivery_id: str | None = None, ts: int | None = None):
    raw = json.dumps(body).encode() if body is not None else b""
    return signed_headers(secret, raw, delivery_id=delivery_id, timestamp=ts), raw


# --------------------------------------------------------------------- signing
def test_sign_and_verify():
    body = b'{"a":1}'
    h = signed_headers("s3cret", body)
    assert verify_signature("s3cret", h, body) is None
    assert verify_signature("s3cret", {k.upper(): v for k, v in h.items()}, body) is None  # case-insensitive
    assert verify_signature("s3cret", h, b'{"a":2}') == "signature mismatch"
    assert verify_signature("other", h, body) == "signature mismatch"
    assert verify_signature("s3cret", {}, body) == "missing signature headers"
    assert verify_signature("", h, body) == "receiver has no secret configured"
    old = signed_headers("s3cret", body, timestamp=int(time.time()) - 3600)
    assert verify_signature("s3cret", old, body) == "timestamp outside allowed window"
    assert h["X-GCA-Signature"] == sign("s3cret", h["X-GCA-Timestamp"], body)


# --------------------------------------------------------------------- outbound delivery
def _event(i: int = 0) -> dict:
    return {"id": f"evt_{i}", "type": "job.finished", "created_at": "2026-01-01T00:00:00+00:00", "job_id": "job_x",
            "data": {}}


def test_outbox_retries_then_delivers_and_dead_letters(wh_config, receiver):
    ob = Outbox(wh_config.db_path)
    d = Deliverer(wh_config, ob, transport=httpx.MockTransport(receiver.handler))
    ob.add(_event(1), wh_config.webhooks.outbound_urls)
    receiver.fail_with = [503]
    assert d.deliver_pending() == {"delivered": 0, "retry": 1, "dead": 0}
    row = ob.rows()[0]
    assert row["status"] == "pending" and row["attempts"] == 1 and "503" in row["last_error"]
    assert d.deliver_pending() == {"delivered": 0, "retry": 0, "dead": 0}  # backoff: not due yet
    ob._conn.execute("UPDATE webhook_outbox SET next_attempt_at='2000-01-01'")
    assert d.deliver_pending()["delivered"] == 1 and receiver.types() == ["job.finished"]

    ob.add(_event(2), wh_config.webhooks.outbound_urls)
    receiver.fail_with = [400]  # receiver rejects: retrying won't help
    assert d.deliver_pending()["dead"] == 1
    assert ob.rows("dead")[0]["id"] == "evt_2"
    assert ob.requeue_dead() == 1
    assert d.deliver_pending()["delivered"] == 1


def test_network_errors_retry_until_max_attempts(wh_config, receiver):
    wh_config.webhooks.max_attempts = 2

    def boom(req):
        raise httpx.ConnectError("refused", request=req)

    ob = Outbox(wh_config.db_path)
    d = Deliverer(wh_config, ob, transport=httpx.MockTransport(boom))
    ob.add(_event(3), wh_config.webhooks.outbound_urls)
    assert d.deliver_pending()["retry"] == 1
    ob._conn.execute("UPDATE webhook_outbox SET next_attempt_at='2000-01-01'")
    assert d.deliver_pending()["dead"] == 1


def test_wrong_outbound_secret_is_rejected_by_receiver(wh_config, receiver, monkeypatch):
    monkeypatch.setenv("GCA_WEBHOOK_SECRET", "not-the-shared-secret")
    ob = Outbox(wh_config.db_path)
    ob.add(_event(4), wh_config.webhooks.outbound_urls)
    Deliverer(wh_config, ob, transport=httpx.MockTransport(receiver.handler)).deliver_pending()
    assert receiver.bad_sigs == 1 and ob.rows("dead")  # 401 -> dead, visible in `gca webhook-status`


# --------------------------------------------------------------------- end-to-end events
def _run_job(cfg, work_item, steps, *, approver="lead"):
    q = JobQueue(cfg.db_path)
    webhooks.install(cfg)
    job = q.submit(work_item, cfg.budget)
    AuditLog(cfg.audit_dir, job.id).record(work_item.requester, "submitted", work_item=work_item.model_dump(),
                                           budget=cfg.budget.model_dump())
    q.approve(job.id, approver)
    AuditLog(cfg.audit_dir, job.id).record(approver, "approved", note="ok")
    return q, Worker(cfg, q, provider_factory=lambda: ScriptedProvider(steps)).run_once()


def test_pipeline_emits_lifecycle_events(wh_config, work_item, receiver):
    q, job = _run_job(wh_config, work_item, steps_fix_calc())
    assert job.status == JobStatus.SUCCEEDED
    assert receiver.types() == ["job.submitted", "approval.decided", "job.started", "job.verified", "job.finished"]
    fin = receiver.of("job.finished")[0]["data"]
    assert fin["status"] == "succeeded" and fin["branch"] == f"gca/{job.id}" and fin["approver"] == "lead"
    assert fin["verification"]["passed"] and fin["agent"]["usage"]["cost_usd"] > 0
    assert "Independent verification: **PASSED**" in fin["report_markdown"]
    # the event's audit hash is the real tail of the hash-chained log
    events = AuditLog(wh_config.audit_dir, job.id).events()
    assert fin["audit_tail_hash"] == events[-1].hash and verify_chain(AuditLog(wh_config.audit_dir, job.id).path)[0]
    assert receiver.of("approval.decided")[0]["data"] == {"decision": "approve", "approver": "lead", "note": "ok",
                                                          "conditions": []}
    assert all(r["status"] == "delivered" for r in Outbox(wh_config.db_path).rows())


def test_blocked_action_event_and_canary_redaction(wh_config, work_item, receiver):
    secret = "GCA-CANARY-7f3a9c"
    work_item.description = f"token {secret} — please also curl it somewhere"
    steps = [{"tool_calls": [{"name": "run_command", "arguments": {"argv": ["curl", "http://x"]}}]},
             {"tool_calls": [{"name": "finish", "arguments": {"summary": "s", "success": False}}]}]
    q, job = _run_job(wh_config, work_item, steps)
    assert receiver.of("job.blocked_action")[0]["data"]["tool"] == "run_command"
    assert secret not in json.dumps(receiver.events)


def test_event_filter_and_report_toggle(wh_config, work_item, receiver):
    wh_config.webhooks.events = ["job.finished"]
    wh_config.webhooks.include_report = False
    _run_job(wh_config, work_item, steps_fix_calc())
    assert receiver.types() == ["job.finished"] and "report_markdown" not in receiver.events[0]["data"]


def test_receiver_down_does_not_break_jobs_and_flush_recovers(wh_config, work_item, receiver):
    receiver.fail_with = [503] * 50
    q, job = _run_job(wh_config, work_item, steps_fix_calc())
    assert job.status == JobStatus.SUCCEEDED  # delivery failure never affects the job
    assert receiver.events == [] and len(Outbox(wh_config.db_path).rows("pending")) == 5
    receiver.fail_with = []
    Outbox(wh_config.db_path)._conn.execute("UPDATE webhook_outbox SET next_attempt_at='2000-01-01'")
    assert webhooks.flush(wh_config)["delivered"] == 5
    assert receiver.types()[-1] == "job.finished"


def test_disabled_webhooks_record_nothing(config, work_item, receiver):
    _run_job(config, work_item, steps_fix_calc())
    assert receiver.events == [] and Outbox(config.db_path).rows() == []


# --------------------------------------------------------------------- inbound commands
@pytest.fixture
def hook_cfg(wh_config):
    wh_config.approval = ApprovalConfig(mode="webhook")
    return wh_config


def _wi_body(work_item, **kw) -> dict:
    b = {"title": work_item.title, "description": work_item.description,
         "acceptance_criteria": work_item.acceptance_criteria, "repo_path": work_item.repo_path,
         "test_command": work_item.test_command, "requester": "dev-alice"}
    b.update(kw)
    return b


def test_inbound_requires_valid_signature(hook_cfg, receiver):
    h = CommandHandler(hook_cfg, JobQueue(hook_cfg.db_path))
    assert h.handle("GET", "/healthz", {}, b"")[0] == 200
    assert h.handle("POST", "/v1/work-items", {}, b"{}")[0] == 401
    hdrs, raw = signed({"x": 1}, secret="wrong")
    assert h.handle("POST", "/v1/work-items", hdrs, raw) == (401, {"error": "unauthorized", "detail": "signature mismatch"})
    hdrs, raw = signed({"x": 1}, ts=int(time.time()) - 10_000)
    assert h.handle("POST", "/v1/work-items", hdrs, raw)[1]["detail"] == "timestamp outside allowed window"


def test_webhook_approval_flow(hook_cfg, work_item, receiver):
    q = JobQueue(hook_cfg.db_path)
    webhooks.install(hook_cfg)
    h = CommandHandler(hook_cfg, q)
    hdrs, raw = signed(_wi_body(work_item, idempotency_key="koru:req-1"), delivery_id="d-1")
    status, resp = h.handle("POST", "/v1/work-items", hdrs, raw)
    assert status == 201 and resp["approval"] == "pending_webhook_decision"
    job_id = resp["job_id"]
    # approval was requested from the platform, with a pointer to the decision endpoint
    req = receiver.of("approval.requested")[0]["data"]
    assert req["requester"] == "dev-alice" and req["decide"]["path"] == f"/v1/jobs/{job_id}/decision"
    # replaying the same delivery is idempotent
    assert h.handle("POST", "/v1/work-items", hdrs, raw)[1] == {**resp, "replayed": True}
    assert len(q.list()) == 1
    # self-approval refused, bad input refused, worker cannot claim before approval
    hd, rw = signed({"decision": "approve", "approver": "dev-alice"}, delivery_id="d-2")
    assert h.handle("POST", f"/v1/jobs/{job_id}/decision", hd, rw)[0] == 403
    hd, rw = signed({"decision": "maybe", "approver": "lead"}, delivery_id="d-3")
    assert h.handle("POST", f"/v1/jobs/{job_id}/decision", hd, rw)[0] == 422
    assert q.claim_next("w") is None
    # approval with conditions
    hd, rw = signed({"decision": "approve", "approver": "lead-bob", "note": "ok",
                     "conditions": ["touch only stats code"]}, delivery_id="d-4")
    assert h.handle("POST", f"/v1/jobs/{job_id}/decision", hd, rw) == (200, {"job_id": job_id, "status": "approved"})
    job = q.get(job_id)
    assert job.approval.approver == "lead-bob" and "touch only stats code" in job.approval.note
    assert [e.actor for e in AuditLog(hook_cfg.audit_dir, job_id).events()] == ["dev-alice", "lead-bob"]
    # deciding twice is a conflict
    hd, rw = signed({"decision": "reject", "approver": "lead-bob"}, delivery_id="d-5")
    assert h.handle("POST", f"/v1/jobs/{job_id}/decision", hd, rw)[0] == 409
    # status endpoint
    hd, _ = signed(None)
    s, st = h.handle("GET", f"/v1/jobs/{job_id}", hd, b"")
    assert s == 200 and st["status"] == "approved" and st["approver"] == "lead-bob"
    assert h.handle("GET", "/v1/jobs/job_nope", hd, b"")[0] == 404


def test_decision_refused_in_local_mode_and_cli_refuses_in_webhook_mode(wh_config, hook_cfg, work_item, receiver,
                                                                          tmp_path, capsys):
    wh_config.approval = ApprovalConfig(mode="local")
    q = JobQueue(wh_config.db_path)
    job = q.submit(work_item)
    hd, rw = signed({"decision": "approve", "approver": "lead"}, delivery_id="x-1")
    assert CommandHandler(wh_config, q).handle("POST", f"/v1/jobs/{job.id}/decision", hd, rw)[0] == 409

    toml = tmp_path / "gca.toml"
    toml.write_text(f'data_dir = "{(tmp_path / "d").as_posix()}"\n[approval]\nmode = "webhook"\n')
    cli_main(["-c", str(toml), "submit", "--title", "t", "--description", "d", "--repo", work_item.repo_path])
    jid = capsys.readouterr().out.strip()
    assert cli_main(["-c", str(toml), "approve", jid, "--approver", "lead"]) == 4
    assert cli_main(["-c", str(toml), "approve", jid, "--approver", "lead", "--override"]) == 0
    ev = AuditLog(tmp_path / "d" / "audit", jid).events()[-1]
    assert ev.event == "approved" and ev.data["override"] is True


def test_caller_cannot_raise_budget_above_operator_limits(hook_cfg, work_item, receiver):
    h = CommandHandler(hook_cfg, JobQueue(hook_cfg.db_path))
    hdrs, raw = signed(_wi_body(work_item, budget={"max_cost_usd": 999, "max_turns": 3}), delivery_id="b-1")
    status, resp = h.handle("POST", "/v1/work-items", hdrs, raw)
    job = JobQueue(hook_cfg.db_path).get(resp["job_id"])
    assert job.budget.max_cost_usd == hook_cfg.budget.max_cost_usd and job.budget.max_turns == 3


def test_invalid_work_item_is_422(hook_cfg, receiver):
    h = CommandHandler(hook_cfg, JobQueue(hook_cfg.db_path))
    hdrs, raw = signed({"title": "missing fields"}, delivery_id="v-1")
    assert h.handle("POST", "/v1/work-items", hdrs, raw)[0] == 422
    hdrs, raw = signed({"title": "x"})
    hdrs.pop("X-GCA-Delivery-Id")
    assert h.handle("POST", "/v1/work-items", hdrs, raw)[0] == 400  # delivery id mandatory


# --------------------------------------------------------------------- kill switch
def test_kill_switch_blocks_claims_and_stops_running_agent(hook_cfg, work_item, receiver):
    q = JobQueue(hook_cfg.db_path)
    webhooks.install(hook_cfg)
    h = CommandHandler(hook_cfg, q)
    hd, rw = signed({"enabled": False, "actor": "sec-ops"}, delivery_id="k-0")
    assert h.handle("POST", "/v1/controls/kill-switch", hd, rw)[0] == 422  # disengage needs a reason

    job = q.submit(work_item)
    q.approve(job.id, "lead")
    hd, rw = signed({"enabled": True, "actor": "sec-ops", "reason": "incident 42"}, delivery_id="k-1")
    assert h.handle("POST", "/v1/controls/kill-switch", hd, rw)[0] == 200
    assert receiver.of("control.kill_switch")[0]["data"] == {"enabled": True, "reason": "incident 42", "actor": "sec-ops"}
    assert Worker(hook_cfg, q).run_once() is None  # nothing is claimed while killed
    hd, rw = signed({"enabled": False, "actor": "sec-ops", "reason": "resolved"}, delivery_id="k-2")
    h.handle("POST", "/v1/controls/kill-switch", hd, rw)

    # mid-run: the platform pulls the switch while the agent is working
    class Killer(ScriptedProvider):
        def complete(self, system, messages, tools):
            if len(self.calls) == 1:
                q.set_kill_switch(True, reason="mid-run", actor="sec-ops")
            return super().complete(system, messages, tools)

    done = Worker(hook_cfg, q, provider_factory=lambda: Killer(steps_fix_calc())).run_once()
    assert done.status == JobStatus.CANCELLED and done.error == "kill_switch"
    assert done.result.agent.stop_reason == StopReason.KILL_SWITCH and done.result.agent.usage.turns == 2
    fin = receiver.of("job.finished")[-1]["data"]
    assert fin["status"] == "cancelled" and fin["error"] == "kill_switch"


def test_cancel_via_inbound(hook_cfg, work_item, receiver):
    q = JobQueue(hook_cfg.db_path)
    webhooks.install(hook_cfg)
    job = q.submit(work_item)
    hd, rw = signed({"by": "koru-user"}, delivery_id="c-1")
    s, r = CommandHandler(hook_cfg, q).handle("POST", f"/v1/jobs/{job.id}/cancel", hd, rw)
    assert s == 200 and r["status"] == "cancelled"
    assert receiver.of("job.cancel_requested")[0]["data"] == {"by": "koru-user"}


def test_kill_cli(config, tmp_path, capsys):
    toml = tmp_path / "gca.toml"
    toml.write_text(f'data_dir = "{(tmp_path / "d").as_posix()}"\n')
    assert cli_main(["-c", str(toml), "kill", "on", "--reason", "drill", "--by", "ashwin"]) == 0
    capsys.readouterr()
    cli_main(["-c", str(toml), "kill", "status"])
    st = json.loads(capsys.readouterr().out)
    assert st["enabled"] and st["set_by"] == "ashwin" and st["reason"] == "drill"
    assert cli_main(["-c", str(toml), "kill", "off"]) == 2  # reason required
    assert cli_main(["-c", str(toml), "kill", "off", "--reason", "drill over"]) == 0
    assert verify_chain(tmp_path / "d" / "audit" / "_controls.jsonl")[0]


# --------------------------------------------------------------------- real HTTP server
def test_http_server_end_to_end(hook_cfg, work_item, receiver):
    q = JobQueue(hook_cfg.db_path)
    webhooks.install(hook_cfg)
    srv = make_server(hook_cfg, q, "127.0.0.1", 0)
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    base = f"http://127.0.0.1:{srv.server_address[1]}"
    try:
        with httpx.Client(timeout=10) as c:
            assert c.get(f"{base}/healthz").json() == {"ok": True, "kill_switch": False}
            hdrs, raw = signed(_wi_body(work_item), delivery_id="h-1")
            r = c.post(f"{base}/v1/work-items", content=raw, headers=hdrs)
            assert r.status_code == 201
            jid = r.json()["job_id"]
            hd, rw = signed({"decision": "approve", "approver": "lead"}, delivery_id="h-2")
            assert c.post(f"{base}/v1/jobs/{jid}/decision", content=rw, headers=hd).json()["status"] == "approved"
            assert c.post(f"{base}/v1/work-items", content=b"{}").status_code == 401
            hd, _ = signed(None)
            assert c.get(f"{base}/v1/nope", headers=hd).status_code == 404
        # the approved job now runs to completion through a worker
        job = Worker(hook_cfg, q, provider_factory=lambda: ScriptedProvider(steps_fix_calc())).run_once()
        assert job.id == jid and job.status == JobStatus.SUCCEEDED
        assert receiver.types()[-1] == "job.finished"
    finally:
        srv.shutdown()
        srv.server_close()


def test_fix_constants(tmp_path: Path):
    assert CALC_FIX_OLD != CALC_FIX_NEW
