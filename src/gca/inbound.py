"""Inbound webhook commands: let an external platform (e.g. Koru) drive gca over signed HTTP.

    POST /v1/work-items              submit a work item                      -> 201 {"job_id", "status"}
    POST /v1/jobs/{id}/decision      approve | reject (approval.mode=webhook) -> 200 {"job_id", "status"}
    POST /v1/jobs/{id}/cancel        request cancellation                    -> 200 {"job_id", "status", ...}
    POST /v1/controls/kill-switch    {"enabled", "reason", "actor", "scope"} -> 200 {"enabled", ...}
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
import threading
import time
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

from pydantic import ValidationError

from .approvals import approval_deadline, expire_overdue
from .audit import AuditLog
from .config import Config
from .models import (
    AGENT_ID_PATTERN,
    Budget,
    JobStatus,
    Restrictions,
    WorkItem,
    add_column_if_missing,
    now_iso,
    open_db,
)
from .policy import PolicyViolation, check_repo_allowed
from .privacy import IdentityError, check_identity
from .publish import publish_job
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
        self._conn = open_db(self, str(cfg.db_path))
        self._conn.execute("PRAGMA busy_timeout=5000")
        self._conn.execute(
            """CREATE TABLE IF NOT EXISTS inbound_deliveries (
                   id TEXT PRIMARY KEY, received_at TEXT NOT NULL, status INTEGER NOT NULL, response TEXT NOT NULL)""")
        # received_at is the lease (renewed when a copy takes over an abandoned reservation); first_received_at never
        # changes and is what supersession checks compare against
        add_column_if_missing(self._conn, "inbound_deliveries", "first_received_at", "TEXT")
        self._conn.execute("UPDATE inbound_deliveries SET first_received_at=received_at WHERE first_received_at IS NULL")
        self._lock = threading.Lock()

    def _audit(self, job_id: str) -> AuditLog:
        return AuditLog(self.cfg.audit_dir, job_id, redactions=self.cfg.policy.canary_strings)

    def _identity(self, value: str | None, field: str) -> str:
        """A caller-supplied person identity; an opaque user id when `[privacy] opaque_identities` is on."""
        try:
            return check_identity(self.cfg, value, field)
        except IdentityError as e:
            raise CommandError(422, str(e)) from e

    # ------------------------------------------------------------- dispatch
    def handle(self, method: str, path: str, headers: dict[str, str], body: bytes) -> tuple[int, dict[str, Any]]:
        if method == "GET" and path == "/healthz":
            return 200, {"ok": True, "kill_switch": bool(self.queue.kill_switch())}
        reason = verify_signature(self.secret, method, path, headers, body,
                                  max_skew_seconds=self.cfg.webhooks.max_skew_seconds)
        if reason:
            return 401, {"error": "unauthorized", "detail": reason}
        delivery_id = {k.lower(): v for k, v in headers.items()}.get("x-gca-delivery-id", "")
        first_received_at: str | None = None
        if method == "POST":
            if not delivery_id:
                return 400, {"error": "X-GCA-Delivery-Id header is required for POST"}
            # Reserve the delivery id *before* doing anything, so two concurrent copies of the same delivery
            # cannot both run the command. The reservation row (status 0) is filled in when the command is done.
            reserved, first_received_at, _ = self._reserve(delivery_id)
            if not reserved:
                return self._replay(delivery_id)
        try:
            payload = json.loads(body or b"{}") if method == "POST" else {}
            if not isinstance(payload, dict):
                raise CommandError(400, "body must be a JSON object")
            status, resp = self._route(method, path, payload, delivery_id, first_received_at)
        except CommandError as e:
            status, resp = e.status, {"error": e.message}
        except json.JSONDecodeError:
            status, resp = 400, {"error": "invalid JSON"}
        except ValidationError as e:  # which field and why, never the submitted value (it may be personal data)
            status, resp = 422, {"error": "validation failed",
                                 "detail": [{k: v for k, v in err.items() if k not in ("input", "url")}
                                            for err in json.loads(e.json())[:10]]}
        except Exception:
            if method == "POST":
                self._release(delivery_id)
            raise
        if method == "POST":
            if status < 500:  # (a kill-switch command has already recorded itself atomically; this is a no-op then)
                with self._lock:
                    self._conn.execute("UPDATE inbound_deliveries SET status=?, response=? WHERE id=? AND status=0",
                                       (status, json.dumps(resp, default=str), delivery_id))
            else:
                self._release(delivery_id)
        return status, resp

    def _release(self, delivery_id: str) -> None:
        """The command did not complete: let a retry take the delivery over right away. The row is kept, not
        deleted, so its first-received time survives and a later retry is still judged against it."""
        with self._lock:
            self._conn.execute("UPDATE inbound_deliveries SET received_at='1970-01-01T00:00:00+00:00' "
                               "WHERE id=? AND status=0", (delivery_id,))

    # A reservation (status 0) older than this belongs to a process that died mid-command: it is taken over by
    # the next copy of the delivery, which re-runs the command. Every command is safe to re-run: submit is
    # idempotent per delivery id, a second decision is a 409, cancel and the kill switch are idempotent.
    RESERVATION_TTL_SECONDS = 120

    def _reserve(self, delivery_id: str) -> tuple[bool, str | None, str | None]:
        """(reserved, first_received_at, taken_over_since).

        first_received_at is the immutable time the delivery id was first seen (now, for a fresh one).
        taken_over_since is set when this copy took over a delivery whose earlier copy died mid-command."""
        with self._lock:
            now = now_iso()
            if self._conn.execute("INSERT OR IGNORE INTO inbound_deliveries (id, received_at, status, response, "
                                  "first_received_at) VALUES (?,?,0,'',?)", (delivery_id, now, now)).rowcount == 1:
                return True, now, None
            cutoff = (datetime.now(timezone.utc) - timedelta(seconds=self.RESERVATION_TTL_SECONDS)).isoformat()
            row = self._conn.execute(
                "SELECT received_at, first_received_at FROM inbound_deliveries WHERE id=? AND status=0 AND received_at<?",
                (delivery_id, cutoff)).fetchone()
            if row is None:
                return False, None, None
            # take over the abandoned reservation: renew the lease only, never the first-received time
            won = self._conn.execute(
                "UPDATE inbound_deliveries SET received_at=? WHERE id=? AND status=0 AND received_at=?",
                (now, delivery_id, row[0])).rowcount == 1
            return won, (row[1] or row[0]) if won else None, (row[0] if won else None)

    def _replay(self, delivery_id: str, wait_seconds: float = 5.0) -> tuple[int, dict[str, Any]]:
        """The stored response for a delivery id seen before; waits briefly for one still in progress."""
        deadline = time.monotonic() + wait_seconds
        while True:
            row = self._conn.execute("SELECT status, response FROM inbound_deliveries WHERE id=?",
                                     (delivery_id,)).fetchone()
            if row is None:
                return 409, {"error": "delivery is being retried", "delivery_id": delivery_id}
            if int(row[0]) != 0:
                return int(row[0]), {**json.loads(row[1]), "replayed": True}
            if time.monotonic() >= deadline:
                return 409, {"error": "delivery in progress", "delivery_id": delivery_id,
                             "retry_after_seconds": self.RESERVATION_TTL_SECONDS}
            time.sleep(0.05)

    def _route(self, method: str, path: str, p: dict[str, Any], delivery_id: str = "",
               first_received_at: str | None = None) -> tuple[int, dict[str, Any]]:
        if method == "POST" and path == "/v1/work-items":
            return self.submit(p, delivery_id)
        if method == "POST" and path == "/v1/controls/kill-switch":
            return self.kill_switch(p, delivery_id, first_received_at)
        m = re.fullmatch(r"/v1/jobs/([A-Za-z0-9_\-]+)(/decision|/cancel|/publish)?", path)
        if m:
            job_id, action = m.group(1), m.group(2)
            if method == "GET" and action is None:
                return self.status(job_id)
            if method == "POST" and action == "/decision":
                return self.decision(job_id, p)
            if method == "POST" and action == "/cancel":
                return self.cancel(job_id, p)
            if method == "POST" and action == "/publish":
                return self.publish(job_id, p)
        raise CommandError(404, f"no route for {method} {path}")

    # ------------------------------------------------------------- commands
    def submit(self, p: dict[str, Any], delivery_id: str = "") -> tuple[int, dict[str, Any]]:
        budget_in = p.pop("budget", None)
        timeout_in = p.pop("approval_timeout_seconds", None)
        p.setdefault("source", "webhook")
        if delivery_id and not p.get("idempotency_key"):
            p["idempotency_key"] = f"delivery:{delivery_id}"  # a retried delivery can never create a second job
        item = WorkItem.model_validate(p)
        if self.cfg.privacy.opaque_identities:
            item.requester = self._identity(p.get("requester"), "requester")
        try:
            expires_at = approval_deadline(self.cfg, timeout_in)
        except ValueError as e:
            raise CommandError(422, str(e)) from e
        try:
            item.repo_path = check_repo_allowed(self.cfg.policy.allowed_repos, item.repo_path)
        except PolicyViolation as e:
            raise CommandError(403, str(e)) from e
        budget = self.cfg.budget.model_copy(update=Budget.model_validate(budget_in).model_dump(exclude_unset=True)) \
            if budget_in else self.cfg.budget
        # a caller can lower limits but never raise them above the operator's configured budget
        for f in ("max_turns", "max_input_tokens", "max_output_tokens", "max_cost_usd", "max_seconds",
                  "max_tool_calls", "max_files_changed", "max_lines_changed"):
            setattr(budget, f, min(getattr(budget, f), getattr(self.cfg.budget, f)))
        job = self.queue.submit(item, budget, approval_expires_at=expires_at)
        if job.status == JobStatus.SUBMITTED and not self._audit(job.id).events():
            self._audit(job.id).record(item.requester, "submitted", work_item=item.model_dump(),
                                       budget=budget.model_dump(), approval_expires_at=job.approval_expires_at,
                                       via="webhook")
        out: dict[str, Any] = {"job_id": job.id, "status": job.status.value,
                               "approval": "pending_webhook_decision" if self.cfg.approval.mode == "webhook" else "local"}
        if job.approval_expires_at:
            out["approval_expires_at"] = job.approval_expires_at
        if job.work_item.agent_id and self.queue.kill_switch(job.work_item.agent_id):
            out["agent_stopped"] = True  # accepted, but held until that agent's switch is released
        return 201, out

    def decision(self, job_id: str, p: dict[str, Any]) -> tuple[int, dict[str, Any]]:
        if self.cfg.approval.mode != "webhook":
            raise CommandError(409, "approval.mode is not 'webhook'; decisions are taken locally")
        self._job(job_id)
        # a decision that arrives after the window closed finds the job expired (and the platform hears so)
        expire_overdue(self.cfg, self.queue, job_id=job_id)
        job = self._job(job_id)
        if job.status == JobStatus.CANCELLED and job.error == "approval_expired":
            raise CommandError(409, f"approval expired: the window for {job_id} closed at {job.approval_expires_at}")
        decision, approver = p.get("decision"), (p.get("approver") or "").strip()
        if decision not in ("approve", "reject"):
            raise CommandError(422, "decision must be 'approve' or 'reject'")
        if not approver:
            raise CommandError(422, "approver is required")
        approver = self._identity(approver, "approver")
        if approver == job.work_item.requester and not self.cfg.approval.allow_self_approval:
            raise CommandError(403, "self-approval is not allowed")
        note = str(p.get("note", ""))
        conditions = [str(c) for c in p.get("conditions", [])][:20]  # free text, recorded for humans
        restrictions = None  # structured, enforced
        if p.get("restrictions") is not None:
            if not isinstance(p["restrictions"], dict):
                raise CommandError(422, "restrictions must be an object")
            restrictions = Restrictions.model_validate(p["restrictions"])
        try:
            if decision == "approve":
                job = self.queue.approve(job_id, approver, note, conditions=conditions, restrictions=restrictions,
                                         operator_commands=self.cfg.policy.allowed_commands)
            else:
                job = self.queue.reject(job_id, approver, note)
        except InvalidTransition as e:
            raise CommandError(409, str(e)) from e
        except ValueError as e:  # a restriction that would widen scope or leave nothing allowed
            raise CommandError(422, f"restrictions rejected: {e}") from e
        self._audit(job_id).record(approver, "approved" if decision == "approve" else "rejected", note=note,
                                   conditions=conditions, via="webhook",
                                   restrictions=restrictions.model_dump(exclude_none=True) if restrictions else None)
        out: dict[str, Any] = {"job_id": job_id, "status": job.status.value}
        if job.approval and job.approval.restrictions:
            out["enforced"] = {"allowed_paths": job.work_item.allowed_paths,
                               "allowed_commands": job.approval.restrictions.allowed_commands,
                               "budget": job.budget.model_dump()}
        return 200, out

    def cancel(self, job_id: str, p: dict[str, Any]) -> tuple[int, dict[str, Any]]:
        self._job(job_id)
        by = self._identity(str(p["by"]), "by") if "by" in p else "webhook"  # "webhook": no person named
        try:
            job = self.queue.request_cancel(job_id)
        except InvalidTransition as e:
            raise CommandError(409, str(e)) from e
        self._audit(job_id).record(by, "cancel_requested", via="webhook")
        return 200, {"job_id": job_id, "status": job.status.value, "cancel_requested": job.cancel_requested}

    def publish(self, job_id: str, p: dict[str, Any]) -> tuple[int, dict[str, Any]]:
        """Push the job's verified commit and open a draft pull request, on an explicit, signed, attributed
        request from the platform. The same rules as `gca publish`: the job must have succeeded and its branch
        must still point at the verified commit."""
        if not self.cfg.webhooks.allow_publish:
            raise CommandError(403, "publishing via webhook is disabled ([webhooks] allow_publish = false)")
        job = self._job(job_id)
        actor = str(p.get("actor", "")).strip()
        if not actor:
            raise CommandError(422, "actor is required")
        actor = self._identity(actor, "actor")
        if job.status != JobStatus.SUCCEEDED:
            raise CommandError(409, f"{job_id} is {job.status.value}; only succeeded jobs can be published")
        draft = p.get("draft", self.cfg.publish.draft)
        if not isinstance(draft, bool):
            raise CommandError(422, "draft must be true or false")
        self._audit(job_id).record(actor, "publish_requested", via="webhook", draft=draft)
        try:
            pub = self.cfg.publish
            res = publish_job(self.cfg, self.queue, job_id, remote=str(p.get("remote", pub.remote)),
                              base=p.get("base", pub.base), draft=draft, create_pr=bool(p.get("create_pr", True)),
                              token_env=pub.token_env, api_base=pub.api_base, actor=actor)
        except ValueError as e:  # not publishable in its current state (e.g. the branch moved after verification)
            raise CommandError(409, str(e)) from e
        except RuntimeError as e:  # git or GitHub failure
            raise CommandError(502, f"publish failed: {e}") from e
        return 200, {"job_id": job_id, "branch": res.branch, "pushed_to": res.pushed_to, "pr_url": res.pr_url,
                     "note": res.note}

    def kill_switch(self, p: dict[str, Any], delivery_id: str = "",
                    first_received_at: str | None = None) -> tuple[int, dict[str, Any]]:
        if not isinstance(p.get("enabled"), bool):
            raise CommandError(422, "enabled must be true or false")
        actor, reason = str(p.get("actor", "")).strip(), str(p.get("reason", "")).strip()
        if not actor:
            raise CommandError(422, "actor is required")
        actor = self._identity(actor, "actor")
        # Scopes. This gca instance is one containment scope (run one instance per tenant); "global" and "tenant"
        # both mean this instance. "agent" is one platform agent's switch: its running jobs stop, its queued jobs
        # wait. "job" stops one job: it maps onto cancellation, which also interrupts a command in flight.
        scope = str(p.get("scope", "global"))
        # A target that does not match the scope is a caller bug, and guessing would be dangerous: an agent release
        # sent without scope "agent" would otherwise release the whole instance.
        if p.get("agent_id") not in (None, "") and scope != "agent":
            raise CommandError(422, "agent_id is only valid with scope 'agent'")
        if p.get("job_id") not in (None, "") and scope != "job":
            raise CommandError(422, "job_id is only valid with scope 'job'")
        if scope == "job":
            target = str(p.get("job_id", "")).strip()
            if not target or not p["enabled"]:
                raise CommandError(422, "scope 'job' needs job_id and enabled=true (a job stop cannot be undone)")
            self._job(target)
            try:
                job = self.queue.request_cancel(target)
            except InvalidTransition as e:
                raise CommandError(409, str(e)) from e
            self._audit(target).record(actor, "cancel_requested", via="webhook", reason=reason, scope="job")
            return 200, {"scope": "job", "job_id": target, "status": job.status.value,
                         "cancel_requested": job.cancel_requested}
        if scope not in ("global", "tenant", "agent"):
            raise CommandError(422, "scope must be 'global', 'tenant', 'agent' or 'job'")
        agent_id: str | None = None
        if scope == "agent":
            agent_id = str(p.get("agent_id", "")).strip()
            if not re.fullmatch(AGENT_ID_PATTERN, agent_id):
                raise CommandError(422, "scope 'agent' needs an agent_id (letters, digits and _ . : / -, at most 128)")
        if not p["enabled"] and not reason:
            raise CommandError(422, "a reason is required to disengage the kill switch")
        # The kill switch is the one command whose re-run is not harmless: an old "off" re-applied after a newer
        # "on" would clear an emergency stop. So its supersession check, the control change and the record of this
        # delivery's outcome are one transaction (JobQueue.apply_kill_switch_delivery). The audit entry is written
        # afterwards; if that fails the change and its delivery record are already durable, so a retry replays.
        resp: dict[str, Any] = {"enabled": p["enabled"], "actor": actor, "reason": reason, "scope": scope}
        if agent_id:
            resp["agent_id"] = agent_id
        outcome = self.queue.apply_kill_switch_delivery(
            delivery_id, first_received_at or now_iso(), p["enabled"], reason=reason, actor=actor,
            response=json.dumps(resp, default=str), agent_id=agent_id)
        if outcome == "superseded":
            raise CommandError(409, "superseded: the kill switch was changed after this delivery was first received")
        if outcome == "already_applied":
            return 200, {**resp, "already_applied": True}
        try:
            AuditLog(self.cfg.audit_dir, "_controls").record(actor, "kill_switch", enabled=p["enabled"], reason=reason,
                                                               via="webhook", delivery_id=delivery_id, scope=scope,
                                                               agent_id=agent_id)
        except Exception as e:  # noqa: BLE001 - the control change is committed; say so rather than fail the request
            resp["audit_recorded"] = False
            resp["audit_error"] = str(e)[:200]
        return 200, resp

    def status(self, job_id: str) -> tuple[int, dict[str, Any]]:
        job = self._job(job_id)
        r = job.result
        return 200, {
            "job_id": job.id, "status": job.status.value, "title": job.work_item.title, "error": job.error,
            "cancel_requested": job.cancel_requested, "approver": job.approval.approver if job.approval else None,
            "branch": r.branch if r else None,
            "verification_passed": r.verification.passed if r and r.verification else None,
            "cost_usd": round(r.agent.usage.cost_usd, 6) if r and r.agent else None,
            "agent_id": job.work_item.agent_id, "approval_expires_at": job.approval_expires_at,
            "updated_at": job.updated_at,
        }

    def _job(self, job_id: str):
        try:
            return self.queue.get(job_id)
        except KeyError as e:
            raise CommandError(404, f"unknown job {job_id}") from e


def set_kill_switch(cfg: Config, queue: JobQueue, enabled: bool, *, reason: str, actor: str, via: str,
                    delivery_id: str | None = None, agent_id: str | None = None) -> None:
    """Engage/disengage the instance stop (or one agent's) and record it in the `_controls` audit stream."""
    queue.set_kill_switch(enabled, reason=reason, actor=actor, delivery_id=delivery_id, agent_id=agent_id)
    AuditLog(cfg.audit_dir, "_controls").record(actor, "kill_switch", enabled=enabled, reason=reason, via=via,
                                                scope="agent" if agent_id else "global", agent_id=agent_id)


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
            try:
                status, payload = handler_logic.handle(method, self.path.split("?", 1)[0], dict(self.headers), body)
            except Exception as e:  # noqa: BLE001 - the caller must always get a response, never a dropped socket
                status, payload = 500, {"error": f"internal error: {type(e).__name__}"}
            self._respond(status, payload)

        def do_POST(self) -> None:  # noqa: N802
            self._dispatch("POST")

        def do_GET(self) -> None:  # noqa: N802
            self._dispatch("GET")

        def log_message(self, fmt: str, *args: Any) -> None:  # quiet by default
            pass

    bind = host or cfg.webhooks.inbound_host
    if bind not in ("127.0.0.1", "localhost", "::1") and not cfg.webhooks.behind_tls_proxy:
        raise ValueError(f"refusing to bind the plain-HTTP inbound server on {bind}: put it behind a TLS-terminating "
                         "proxy and set [webhooks] behind_tls_proxy = true, or bind 127.0.0.1")
    return ThreadingHTTPServer((bind, port if port is not None else cfg.webhooks.inbound_port), _Handler)
