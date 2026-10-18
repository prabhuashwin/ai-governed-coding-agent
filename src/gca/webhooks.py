"""Outbound webhooks: signed, durable, at-least-once delivery of job lifecycle events.

How it works
------------
1. Every audit event passes through a sink (`install`). Events we publish are mapped to a public event type
   (`job.finished`, `approval.requested`, ...) and written to an SQLite *outbox* in the same database as the queue.
   Recording never waits on the network and never fails because a receiver is down.
2. Delivery (`Deliverer.deliver_pending`) POSTs each pending event to every configured URL, signed with
   HMAC-SHA256. 2xx = delivered; 408/429/5xx/network errors = retried with exponential backoff; other 4xx =
   dead (the receiver rejected it; retrying will not help). Delivery is triggered inline (best effort), after
   each job by the worker, every few seconds by `gca webhook-serve`, and on demand by `gca webhook-deliver`.

Signature scheme (same for inbound and outbound)
------------------------------------------------
    X-GCA-Timestamp: <unix seconds>
    X-GCA-Signature: sha256=<hex HMAC-SHA256(secret, "<timestamp>.<raw body>")>
    X-GCA-Delivery-Id: <unique id>      (outbound: event id; inbound: caller-chosen, used for idempotency)
    X-GCA-Event: <event type>           (outbound only)
Receivers must reject timestamps outside +/- max_skew_seconds and compare signatures in constant time.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import sqlite3
import threading
import time
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import httpx

from . import audit as _audit
from .config import Config
from .models import AuditEvent, Job, now_iso, open_db

EVENT_TYPES = (
    "job.submitted", "approval.requested", "approval.decided", "job.started", "job.blocked_action",
    "job.verified", "job.finished", "job.cancel_requested", "control.kill_switch",
)

# audit event name -> public event type
_AUDIT_MAP = {
    "submitted": "job.submitted",
    "approved": "approval.decided",
    "rejected": "approval.decided",
    "claimed": "job.started",
    "blocked_action": "job.blocked_action",
    "verified": "job.verified",
    "finished": "job.finished",
    "cancel_requested": "job.cancel_requested",
    "kill_switch": "control.kill_switch",
}

RETRYABLE = {408, 425, 429}


# ----------------------------------------------------------------------------- signing
def sign(secret: str, timestamp: str, body: bytes) -> str:
    mac = hmac.new(secret.encode("utf-8"), timestamp.encode("ascii") + b"." + body, hashlib.sha256)
    return "sha256=" + mac.hexdigest()


def signed_headers(secret: str, body: bytes, *, delivery_id: str | None = None, event: str | None = None,
                   timestamp: int | None = None) -> dict[str, str]:
    ts = str(int(timestamp if timestamp is not None else time.time()))
    h = {"Content-Type": "application/json", "X-GCA-Timestamp": ts, "X-GCA-Signature": sign(secret, ts, body),
         "X-GCA-Delivery-Id": delivery_id or f"dlv_{uuid.uuid4().hex}", "User-Agent": "gca-webhooks/1"}
    if event:
        h["X-GCA-Event"] = event
    return h


def verify_signature(secret: str, headers: dict[str, str], body: bytes, *, max_skew_seconds: int = 300,
                     now: float | None = None) -> str | None:
    """Return None if valid, else a short reason. Header lookup is case-insensitive."""
    low = {k.lower(): v for k, v in headers.items()}
    ts, sig = low.get("x-gca-timestamp"), low.get("x-gca-signature")
    if not secret:
        return "receiver has no secret configured"
    if not ts or not sig:
        return "missing signature headers"
    try:
        age = abs((now if now is not None else time.time()) - int(ts))
    except ValueError:
        return "bad timestamp"
    if age > max_skew_seconds:
        return "timestamp outside allowed window"
    if not hmac.compare_digest(sign(secret, ts, body), sig):
        return "signature mismatch"
    return None


# ----------------------------------------------------------------------------- outbox
class Outbox:
    def __init__(self, db_path: str | Path):
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = open_db(self, str(self.db_path))
        self._conn.execute("PRAGMA busy_timeout=5000")
        self._conn.execute(
            """CREATE TABLE IF NOT EXISTS webhook_outbox (
                   id TEXT NOT NULL, url TEXT NOT NULL, type TEXT NOT NULL, job_id TEXT, payload TEXT NOT NULL,
                   created_at TEXT NOT NULL, attempts INTEGER NOT NULL DEFAULT 0, next_attempt_at TEXT NOT NULL,
                   status TEXT NOT NULL DEFAULT 'pending', last_error TEXT, delivered_at TEXT,
                   PRIMARY KEY (id, url))""")
        self._conn.execute("CREATE INDEX IF NOT EXISTS idx_outbox_due ON webhook_outbox(status, next_attempt_at)")
        self._lock = threading.Lock()

    def add(self, event: dict[str, Any], urls: list[str]) -> None:
        body = json.dumps(event, separators=(",", ":"), ensure_ascii=False, default=str)
        with self._lock:
            for url in urls:
                self._conn.execute(
                    "INSERT OR IGNORE INTO webhook_outbox (id, url, type, job_id, payload, created_at, next_attempt_at) "
                    "VALUES (?,?,?,?,?,?,?)",
                    (event["id"], url, event["type"], event.get("job_id"), body, event["created_at"], event["created_at"]))

    def due(self, limit: int = 100, event_id: str | None = None) -> list[tuple]:
        # 'inflight' rows whose lease has expired belong to a deliverer that died mid-POST: retry them
        q = ("SELECT id, url, type, payload, attempts FROM webhook_outbox "
             "WHERE status IN ('pending','inflight') AND next_attempt_at <= ?")
        args: list[Any] = [now_iso()]
        if event_id:
            q += " AND id = ?"
            args.append(event_id)
        q += " ORDER BY created_at LIMIT ?"
        args.append(limit)
        return self._conn.execute(q, args).fetchall()

    def claim(self, event_id: str, url: str, lease_seconds: float) -> bool:
        """Atomically take one due row for delivery. Several deliverers (the inline publisher, the background
        flusher, a worker, another process) may see the same due row; only the one whose UPDATE wins posts it."""
        lease = (datetime.now(timezone.utc) + timedelta(seconds=lease_seconds)).isoformat()
        with self._lock:
            cur = self._conn.execute(
                "UPDATE webhook_outbox SET status='inflight', next_attempt_at=? "
                "WHERE id=? AND url=? AND status IN ('pending','inflight') AND next_attempt_at <= ?",
                (lease, event_id, url, now_iso()))
            return cur.rowcount == 1

    def mark(self, event_id: str, url: str, *, status: str, attempts: int, error: str | None = None,
             retry_in: float | None = None) -> None:
        nxt = (datetime.now(timezone.utc) + timedelta(seconds=retry_in or 0)).isoformat()
        with self._lock:
            self._conn.execute(
                "UPDATE webhook_outbox SET status=?, attempts=?, last_error=?, next_attempt_at=?, "
                "delivered_at=CASE WHEN ?='delivered' THEN ? ELSE delivered_at END WHERE id=? AND url=?",
                (status, attempts, (error or "")[:500], nxt, status, now_iso(), event_id, url))

    def rows(self, status: str | None = None) -> list[dict[str, Any]]:
        q = "SELECT id, url, type, job_id, attempts, status, last_error, created_at, delivered_at FROM webhook_outbox"
        args: list[Any] = []
        if status:
            q += " WHERE status=?"
            args.append(status)
        cols = ["id", "url", "type", "job_id", "attempts", "status", "last_error", "created_at", "delivered_at"]
        return [dict(zip(cols, r, strict=True)) for r in self._conn.execute(q + " ORDER BY created_at", args)]

    def requeue_dead(self) -> int:
        with self._lock:
            cur = self._conn.execute(
                "UPDATE webhook_outbox SET status='pending', attempts=0, next_attempt_at=? WHERE status='dead'",
                (now_iso(),))
            return cur.rowcount


class Deliverer:
    def __init__(self, cfg: Config, outbox: Outbox, *, transport: httpx.BaseTransport | None = None):
        self.cfg = cfg
        self.outbox = outbox
        self.secret = os.environ.get(cfg.webhooks.outbound_secret_env, "")
        self._transport = transport

    def _backoff(self, attempts: int) -> float:
        return float(min(2 ** attempts * 5, 3600))  # 10s, 20s, 40s ... capped at 1h

    def deliver_pending(self, *, limit: int = 100, event_id: str | None = None,
                        timeout: float | None = None) -> dict[str, int]:
        stats = {"delivered": 0, "retry": 0, "dead": 0}
        rows = self.outbox.due(limit, event_id)
        if not rows:
            return stats
        with httpx.Client(timeout=timeout or self.cfg.webhooks.timeout_seconds, transport=self._transport) as c:
            lease = 2 * (timeout or self.cfg.webhooks.timeout_seconds) + 30
            for eid, url, etype, payload, attempts in rows:
                if not self.outbox.claim(eid, url, lease):
                    continue  # someone else is delivering (or just delivered) this one
                body = payload.encode("utf-8")
                attempts += 1
                try:
                    r = c.post(url, content=body,
                               headers=signed_headers(self.secret, body, delivery_id=eid, event=etype))
                    code = r.status_code
                    err = None if 200 <= code < 300 else f"HTTP {code}: {r.text[:200]}"
                except (httpx.TransportError, httpx.TimeoutException) as e:
                    code, err = 0, f"{type(e).__name__}: {e}"
                if err is None:
                    self.outbox.mark(eid, url, status="delivered", attempts=attempts)
                    stats["delivered"] += 1
                elif (code == 0 or code >= 500 or code in RETRYABLE) and attempts < self.cfg.webhooks.max_attempts:
                    self.outbox.mark(eid, url, status="pending", attempts=attempts, error=err,
                                     retry_in=self._backoff(attempts))
                    stats["retry"] += 1
                else:
                    self.outbox.mark(eid, url, status="dead", attempts=attempts, error=err)
                    stats["dead"] += 1
        return stats


# ----------------------------------------------------------------------------- event building
def _load_job(cfg: Config, job_id: str) -> Job | None:
    try:
        conn = sqlite3.connect(str(cfg.db_path))
        try:
            row = conn.execute("SELECT body FROM jobs WHERE id=?", (job_id,)).fetchone()
        finally:
            conn.close()
        return Job.model_validate_json(row[0]) if row else None
    except sqlite3.Error:
        return None


def build_event(cfg: Config, ev: AuditEvent) -> dict[str, Any] | None:
    etype = _AUDIT_MAP.get(ev.event)
    if etype is None:
        return None
    d = ev.data
    data: dict[str, Any]
    if etype == "job.submitted":
        wi = d.get("work_item", {})
        data = {"work_item": {k: wi.get(k) for k in ("id", "title", "description", "acceptance_criteria", "repo_path",
                                                    "base_ref", "allowed_paths", "requester", "source", "source_ref")},
                "budget": d.get("budget")}
    elif etype == "approval.decided":
        data = {"decision": "approve" if ev.event == "approved" else "reject", "approver": ev.actor,
                "note": d.get("note", ""), "conditions": d.get("conditions", [])}
    elif etype == "job.started":
        job = _load_job(cfg, ev.job_id)
        data = {"worker": ev.actor.removeprefix("worker:"), "attempt": d.get("attempt"),
                "branch": f"gca/{ev.job_id}", "title": job.work_item.title if job else None}
    elif etype == "job.blocked_action":
        data = {"tool": d.get("tool"), "args": d.get("args"), "reason": d.get("result_head", "")}
    elif etype == "job.verified":
        data = {k: d.get(k) for k in ("passed", "flags", "exit_code", "files_changed")}
    elif etype == "job.finished":
        job = _load_job(cfg, ev.job_id)
        data = {"status": d.get("status"), "error": d.get("error"), "audit_tail_hash": ev.hash}
        if job and job.result:
            r = job.result
            a, v = r.agent, r.verification
            data.update({
                "title": job.work_item.title, "branch": r.branch, "provider": r.provider, "model": r.model,
                "requester": job.work_item.requester, "approver": job.approval.approver if job.approval else None,
                "source": job.work_item.source, "source_ref": job.work_item.source_ref,
                "agent": {"stop_reason": a.stop_reason.value, "claimed_success": a.claimed_success,
                          "blocked_actions": a.blocked_actions, "usage": a.usage.model_dump()} if a else None,
                "verification": {"passed": v.passed, "flags": v.flags, "exit_code": v.exit_code,
                                 "files_changed": v.files_changed, "insertions": v.insertions,
                                 "deletions": v.deletions} if v else None,
            })
            if cfg.webhooks.include_report:
                data["report_markdown"] = (r.report_markdown or "")[:60000]
    elif etype == "control.kill_switch":
        data = {"enabled": d.get("enabled"), "reason": d.get("reason", ""), "actor": ev.actor}
    else:  # job.cancel_requested
        data = {"by": ev.actor}
    event = {"id": f"evt_{uuid.uuid4().hex}", "type": etype, "created_at": now_iso(), "source": cfg.webhooks.source_name,
             "job_id": ev.job_id if not ev.job_id.startswith("_") else None, "audit_seq": ev.seq, "data": data}
    # never let a planted secret leave via a webhook
    if cfg.policy.canary_strings:
        text = json.dumps(event, default=str, ensure_ascii=False)
        for c in cfg.policy.canary_strings:
            if c:
                text = text.replace(c, "[REDACTED]")
        event = json.loads(text)
    return event


def approval_requested_event(cfg: Config, job: Job) -> dict[str, Any]:
    wi = job.work_item
    return {"id": f"evt_{uuid.uuid4().hex}", "type": "approval.requested", "created_at": now_iso(),
            "source": cfg.webhooks.source_name, "job_id": job.id, "audit_seq": None,
            "data": {"title": wi.title, "description": wi.description[:4000], "acceptance_criteria": wi.acceptance_criteria,
                     "requester": wi.requester, "repo_path": wi.repo_path, "allowed_paths": wi.allowed_paths,
                     "budget": job.budget.model_dump(), "source": wi.source, "source_ref": wi.source_ref,
                     "decide": {"method": "POST", "path": f"/v1/jobs/{job.id}/decision",
                                "body": {"decision": "approve|reject", "approver": "<user>", "note": "", "conditions": []}}}}


# ----------------------------------------------------------------------------- wiring
class WebhookPublisher:
    def __init__(self, cfg: Config, *, transport: httpx.BaseTransport | None = None):
        self.cfg = cfg
        self.outbox = Outbox(cfg.db_path)
        self.deliverer = Deliverer(cfg, self.outbox, transport=transport)

    def wanted(self, etype: str) -> bool:
        return not self.cfg.webhooks.events or etype in self.cfg.webhooks.events

    def publish(self, event: dict[str, Any]) -> None:
        if not self.cfg.webhooks.outbound_urls or not self.wanted(event["type"]):
            return
        self.outbox.add(event, self.cfg.webhooks.outbound_urls)
        if self.cfg.webhooks.deliver_inline:
            try:
                self.deliverer.deliver_pending(event_id=event["id"], timeout=min(self.cfg.webhooks.timeout_seconds, 3))
            except Exception:  # noqa: BLE001 - stays pending; a later flush retries
                pass

    def on_audit(self, ev: AuditEvent) -> None:
        event = build_event(self.cfg, ev)
        if event is not None:
            self.publish(event)
        if ev.event == "submitted" and self.cfg.approval.mode == "webhook":
            job = _load_job(self.cfg, ev.job_id)
            if job is not None:
                self.publish(approval_requested_event(self.cfg, job))


_ACTIVE: WebhookPublisher | None = None
_DEFAULT_TRANSPORT: httpx.BaseTransport | None = None  # tests / custom egress (e.g. a proxy transport)


def set_default_transport(transport: httpx.BaseTransport | None) -> None:
    global _DEFAULT_TRANSPORT
    _DEFAULT_TRANSPORT = transport


def install(cfg: Config, *, transport: httpx.BaseTransport | None = None) -> WebhookPublisher | None:
    """Enable publishing for this process if [webhooks] enabled. Idempotent; returns the publisher."""
    global _ACTIVE
    if not cfg.webhooks.enabled:
        _audit.set_sink("webhooks", None)
        _ACTIVE = None
        return None
    _ACTIVE = WebhookPublisher(cfg, transport=transport or _DEFAULT_TRANSPORT)
    _audit.set_sink("webhooks", _ACTIVE.on_audit)
    return _ACTIVE


def uninstall() -> None:
    global _ACTIVE
    _audit.set_sink("webhooks", None)
    _ACTIVE = None


def flush(cfg: Config) -> dict[str, int]:
    if _ACTIVE is not None:
        return _ACTIVE.deliverer.deliver_pending()
    return Deliverer(cfg, Outbox(cfg.db_path), transport=_DEFAULT_TRANSPORT).deliver_pending()
