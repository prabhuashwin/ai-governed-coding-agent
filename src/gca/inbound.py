"""Inbound webhook commands: let an external platform (e.g. Koru) drive gca over signed HTTP.

    POST /v1/work-items              submit a work item                      -> 201 {"job_id", "status"}
    POST /v1/jobs/{id}/decision      approve | reject (approval.mode=webhook) -> 200 {"job_id", "status"}
    POST /v1/jobs/{id}/cancel        request cancellation                    -> 200 {"job_id", "status", ...}
    POST /v1/controls/kill-switch    {"enabled": bool, "reason", "actor"}    -> 200 {"enabled", ...}
    GET  /v1/jobs/{id}               job status summary (signed, empty body) -> 200 {...}
    GET  /healthz                    liveness, unsigned                      -> 200 {"ok": true}

Every request except /healthz must carry the HMAC headers described in `webhooks.py`, signed with the inbound
secret. `X-GCA-Delivery-Id` makes POSTs idempotent: a retried delivery returns the stored original response.
The server only changes queue state; jobs are executed by a worker (`gca run`, or `--with-worker`).
"""

from __future__ import annotations

import json
import os
import re
import sqlite3
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

from pydantic import ValidationError

from .audit import AuditLog
from .config import Config
from .models import Budget, JobStatus, WorkItem, now_iso
from .queue import InvalidTransition, JobQueue
from .webhooks import verify_signature

MAX_BODY = 1_000_000


class CommandError(Exception):
    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status, self.message = status, message


class CommandHandler:
    """Pure command logic (no HTTP), so it can be unit-tested directly."""

    def __init__(self, cfg: Config, queue: JobQueue):
        self.cfg = cfg
        self.queue = queue
        self.secret = os.environ.get(cfg.webhooks.inbound_secret_env, "")
        self._conn = sqlite3.connect(str(cfg.db_path), isolation_level=None, check_same_thread=False)
        self._conn.execute("PRAGMA busy_timeout=5000")
        self._conn.execute(
            """CREATE TABLE IF NOT EXISTS inbound_deliveries (
                   id TEXT PRIMARY KEY, received_at TEXT NOT NULL, status INTEGER NOT NULL, response TEXT NOT NULL)""")
        self._lock = threading.Lock()

    def _audit(self, job_id: str) -> AuditLog:
        return AuditLog(self.cfg.audit_dir, job_id, redactions=self.cfg.policy.canary_strings)

    # ------------------------------------------------------------- dispatch
    def handle(self, method: str, path: str, headers: dict[str, str], body: bytes) -> tuple[int, dict[str, Any]]:
        if method == "GET" and path == "/healthz":
            return 200, {"ok": True, "kill_switch": bool(self.queue.kill_switch())}
        reason = verify_signature(self.secret, headers, body, max_skew_seconds=self.cfg.webhooks.max_skew_seconds)
        if reason:
            return 401, {"error": "unauthorized", "detail": reason}
        delivery_id = {k.lower(): v for k, v in headers.items()}.get("x-gca-delivery-id", "")
        if method == "POST":
            if not delivery_id:
                return 400, {"error": "X-GCA-Delivery-Id header is required for POST"}
            prior = self._conn.execute("SELECT status, response FROM inbound_deliveries WHERE id=?",
                                       (delivery_id,)).fetchone()
            if prior:
                return int(prior[0]), {**json.loads(prior[1]), "replayed": True}
        try:
            payload = json.loads(body or b"{}") if method == "POST" else {}
            if not isinstance(payload, dict):
                raise CommandError(400, "body must be a JSON object")
            status, resp = self._route(method, path, payload)
        except CommandError as e:
            status, resp = e.status, {"error": e.message}
        except json.JSONDecodeError:
            status, resp = 400, {"error": "invalid JSON"}
        except ValidationError as e:
            status, resp = 422, {"error": "validation failed", "detail": json.loads(e.json())[:10]}
        if method == "POST" and status < 500:
            with self._lock:
                self._conn.execute("INSERT OR IGNORE INTO inbound_deliveries VALUES (?,?,?,?)",
                                   (delivery_id, now_iso(), status, json.dumps(resp, default=str)))
        return status, resp

    def _route(self, method: str, path: str, p: dict[str, Any]) -> tuple[int, dict[str, Any]]:
        if method == "POST" and path == "/v1/work-items":
            return self.submit(p)
        if method == "POST" and path == "/v1/controls/kill-switch":
            return self.kill_switch(p)
        m = re.fullmatch(r"/v1/jobs/([A-Za-z0-9_\-]+)(/decision|/cancel)?", path)
        if m:
            job_id, action = m.group(1), m.group(2)
            if method == "GET" and action is None:
                return self.status(job_id)
            if method == "POST" and action == "/decision":
                return self.decision(job_id, p)
            if method == "POST" and action == "/cancel":
                return self.cancel(job_id, p)
        raise CommandError(404, f"no route for {method} {path}")

    # ------------------------------------------------------------- commands
    def submit(self, p: dict[str, Any]) -> tuple[int, dict[str, Any]]:
        budget_in = p.pop("budget", None)
        p.setdefault("source", "webhook")
        item = WorkItem.model_validate(p)
        budget = self.cfg.budget.model_copy(update=Budget.model_validate(budget_in).model_dump(exclude_unset=True)) \
            if budget_in else self.cfg.budget
        # a caller can lower limits but never raise them above the operator's configured budget
        for f in ("max_turns", "max_input_tokens", "max_output_tokens", "max_cost_usd", "max_seconds",
                  "max_tool_calls", "max_files_changed", "max_lines_changed"):
            setattr(budget, f, min(getattr(budget, f), getattr(self.cfg.budget, f)))
        job = self.queue.submit(item, budget)
        if job.status == JobStatus.SUBMITTED and not self._audit(job.id).events():
            self._audit(job.id).record(item.requester, "submitted", work_item=item.model_dump(),
                                       budget=budget.model_dump(), via="webhook")
        return 201, {"job_id": job.id, "status": job.status.value,
                     "approval": "pending_webhook_decision" if self.cfg.approval.mode == "webhook" else "local"}

    def decision(self, job_id: str, p: dict[str, Any]) -> tuple[int, dict[str, Any]]:
        if self.cfg.approval.mode != "webhook":
            raise CommandError(409, "approval.mode is not 'webhook'; decisions are taken locally")
        job = self._job(job_id)
        decision, approver = p.get("decision"), (p.get("approver") or "").strip()
        if decision not in ("approve", "reject"):
            raise CommandError(422, "decision must be 'approve' or 'reject'")
        if not approver:
            raise CommandError(422, "approver is required")
        if approver == job.work_item.requester and not self.cfg.approval.allow_self_approval:
            raise CommandError(403, "self-approval is not allowed")
        note = str(p.get("note", ""))
        conditions = [str(c) for c in p.get("conditions", [])][:20]
        if conditions:
            note = (note + "\nConditions: " + "; ".join(conditions)).strip()
        try:
            job = self.queue.approve(job_id, approver, note) if decision == "approve" \
                else self.queue.reject(job_id, approver, note)
        except InvalidTransition as e:
            raise CommandError(409, str(e)) from e
        self._audit(job_id).record(approver, "approved" if decision == "approve" else "rejected", note=note,
                                   conditions=conditions, via="webhook")
        return 200, {"job_id": job_id, "status": job.status.value}

    def cancel(self, job_id: str, p: dict[str, Any]) -> tuple[int, dict[str, Any]]:
        self._job(job_id)
        try:
            job = self.queue.request_cancel(job_id)
        except InvalidTransition as e:
            raise CommandError(409, str(e)) from e
        self._audit(job_id).record(str(p.get("by", "webhook")), "cancel_requested", via="webhook")
        return 200, {"job_id": job_id, "status": job.status.value, "cancel_requested": job.cancel_requested}

    def kill_switch(self, p: dict[str, Any]) -> tuple[int, dict[str, Any]]:
        if not isinstance(p.get("enabled"), bool):
            raise CommandError(422, "enabled must be true or false")
        actor, reason = str(p.get("actor", "")).strip(), str(p.get("reason", "")).strip()
        if not actor:
            raise CommandError(422, "actor is required")
        if not p["enabled"] and not reason:
            raise CommandError(422, "a reason is required to disengage the kill switch")
        set_kill_switch(self.cfg, self.queue, p["enabled"], reason=reason, actor=actor, via="webhook")
        return 200, {"enabled": p["enabled"], "actor": actor, "reason": reason}

    def status(self, job_id: str) -> tuple[int, dict[str, Any]]:
        job = self._job(job_id)
        r = job.result
        return 200, {
            "job_id": job.id, "status": job.status.value, "title": job.work_item.title, "error": job.error,
            "cancel_requested": job.cancel_requested, "approver": job.approval.approver if job.approval else None,
            "branch": r.branch if r else None,
            "verification_passed": r.verification.passed if r and r.verification else None,
            "cost_usd": round(r.agent.usage.cost_usd, 6) if r and r.agent else None,
            "updated_at": job.updated_at,
        }

    def _job(self, job_id: str):
        try:
            return self.queue.get(job_id)
        except KeyError as e:
            raise CommandError(404, f"unknown job {job_id}") from e


def set_kill_switch(cfg: Config, queue: JobQueue, enabled: bool, *, reason: str, actor: str, via: str) -> None:
    """Engage/disengage the global stop and record it in a dedicated audit stream."""
    queue.set_kill_switch(enabled, reason=reason, actor=actor)
    AuditLog(cfg.audit_dir, "_controls").record(actor, "kill_switch", enabled=enabled, reason=reason, via=via)


# ----------------------------------------------------------------------------- HTTP adapter
def make_server(cfg: Config, queue: JobQueue, host: str | None = None, port: int | None = None) -> ThreadingHTTPServer:
    handler_logic = CommandHandler(cfg, queue)

    class _Handler(BaseHTTPRequestHandler):
        server_version = "gca-inbound/1"

        def _respond(self, status: int, payload: dict[str, Any]) -> None:
            body = json.dumps(payload, default=str).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _dispatch(self, method: str) -> None:
            length = int(self.headers.get("Content-Length") or 0)
            if length > MAX_BODY:
                self._respond(413, {"error": "body too large"})
                return
            body = self.rfile.read(length) if length else b""
            status, payload = handler_logic.handle(method, self.path.split("?", 1)[0], dict(self.headers), body)
            self._respond(status, payload)

        def do_POST(self) -> None:  # noqa: N802
            self._dispatch("POST")

        def do_GET(self) -> None:  # noqa: N802
            self._dispatch("GET")

        def log_message(self, fmt: str, *args: Any) -> None:  # quiet by default
            pass

    return ThreadingHTTPServer((host or cfg.webhooks.inbound_host, port if port is not None else cfg.webhooks.inbound_port),
                               _Handler)
