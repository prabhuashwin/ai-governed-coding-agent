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
from urllib.parse import urlsplit

import httpx

from . import audit as _audit
from .config import Config
from .models import AuditEvent, Job, now_iso, open_db

EVENT_TYPES = (
    "job.submitted", "approval.requested", "approval.decided", "job.started", "job.blocked_action",
    "job.verified", "job.finished", "job.cancel_requested", "job.published", "control.kill_switch",
    "approval.expired",
)

# audit event name -> public event type
_AUDIT_MAP = {
    "submitted": "job.submitted",
    "approved": "approval.decided",
    "rejected": "approval.decided",
    "approval_expired": "approval.expired",
    "claimed": "job.started",
    "blocked_action": "job.blocked_action",
    "verified": "job.verified",
    "finished": "job.finished",
    "cancel_requested": "job.cancel_requested",
    "kill_switch": "control.kill_switch",
    "branch_pushed": "job.published",
    "pull_request_opened": "job.published",
}

RETRYABLE = {408, 425, 429}


# ----------------------------------------------------------------------------- signing
SIGNATURE_VERSION = "2"


def signing_string(timestamp: str, method: str, path: str, delivery_id: str, body: bytes) -> bytes:
    """What is MACed: every field that gives a request its meaning, newline-separated, body last.

    Binding the method, path and delivery id (not only the timestamp and body, as version 1 did) means a
    signed status GET cannot be replayed as a cancel POST, and a signed approval cannot be redirected to another
    job by changing the unsigned path. The fixed fields may not contain newlines, so the encoding is unambiguous.
    """
    for name, value in (("timestamp", timestamp), ("method", method), ("path", path), ("delivery id", delivery_id)):
        if "\n" in value or "\r" in value:
            raise ValueError(f"{name} may not contain a newline")
    head = "\n".join((SIGNATURE_VERSION, timestamp, method.upper(), path, delivery_id)) + "\n"
    return head.encode("utf-8") + body


def sign(secret: str, timestamp: str, method: str, path: str, delivery_id: str, body: bytes) -> str:
    mac = hmac.new(secret.encode("utf-8"), signing_string(timestamp, method, path, delivery_id, body), hashlib.sha256)
    return "sha256=" + mac.hexdigest()


def request_path(url_or_path: str) -> str:
    """The path component a signature binds: no scheme, host or query; `/` when empty."""
    return urlsplit(url_or_path).path or "/"


def signed_headers(secret: str, method: str, url_or_path: str, body: bytes, *, delivery_id: str | None = None,
                   event: str | None = None, timestamp: int | None = None) -> dict[str, str]:
    ts = str(int(timestamp if timestamp is not None else time.time()))
    did = delivery_id if delivery_id is not None else f"dlv_{uuid.uuid4().hex}"
    h = {"Content-Type": "application/json", "X-GCA-Timestamp": ts,
         "X-GCA-Signature": sign(secret, ts, method, request_path(url_or_path), did, body),
         "X-GCA-Signature-Version": SIGNATURE_VERSION, "X-GCA-Delivery-Id": did, "User-Agent": "gca-webhooks/2"}
    if event:
        h["X-GCA-Event"] = event
    return h


def verify_signature(secret: str, method: str, url_or_path: str, headers: dict[str, str], body: bytes, *,
                     max_skew_seconds: int = 300, now: float | None = None) -> str | None:
    """Return None if valid, else a short reason. `method` and `url_or_path` are the request as received.
    Header lookup is case-insensitive."""
    low = {k.lower(): v for k, v in headers.items()}
    ts, sig, did = low.get("x-gca-timestamp"), low.get("x-gca-signature"), low.get("x-gca-delivery-id", "")
    if not secret:
        return "receiver has no secret configured"
    if not ts or not sig:
        return "missing signature headers"
    if low.get("x-gca-signature-version", SIGNATURE_VERSION) != SIGNATURE_VERSION:
        return "unsupported signature version"
    try:
        age = abs((now if now is not None else time.time()) - int(ts))
    except ValueError:
        return "bad timestamp"
    if age > max_skew_seconds:
        return "timestamp outside allowed window"
    try:
        expected = sign(secret, ts, method, request_path(url_or_path), did, body)
    except ValueError as e:
        return str(e)
    if not hmac.compare_digest(expected, sig):
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

    # An outbox insert can fail on transient database contention. The audit event has already been written by
    # then, so the only alternative to losing the webhook is a durable fallback: one JSON line per event in a
    # spill file next to the database, ingested by the next delivery pass.
    @property
    def spill_path(self) -> Path:
        return self.db_path.with_name("webhook_spill.jsonl")

    def add_or_spill(self, event: dict[str, Any], urls: list[str]) -> bool:
        """Queue the event; on a database error write it to the spill file instead. Returns True if queued."""
        try:
            self.add(event, urls)
            return True
        except sqlite3.Error:
            with self._lock, self.spill_path.open("a", encoding="utf-8") as f:
                f.write(json.dumps({"event": event, "urls": urls}, default=str, ensure_ascii=False) + "\n")
            return False

    def ingest_spill(self) -> int:
        """Move spilled events into the outbox.

        The spill file is first renamed to a private draining file (atomic, so concurrent drainers and
        concurrent appenders never touch the same file), and that file is deleted only after every line is in
        the database. A crash mid-way leaves the draining file, which the next pass picks up again; the
        INSERT OR IGNORE on the event id makes re-ingesting an already-inserted line harmless.
        """
        moved = 0
        draining: list[Path] = sorted(self.db_path.parent.glob(self.spill_path.name + ".draining-*"))
        if self.spill_path.exists():
            mine = self.spill_path.with_name(f"{self.spill_path.name}.draining-{uuid.uuid4().hex}")
            try:
                self.spill_path.rename(mine)
                draining.append(mine)
            except OSError:  # another process renamed it first
                pass
        for path in draining:
            try:
                lines = [ln for ln in path.read_text(encoding="utf-8").splitlines() if ln.strip()]
            except OSError:
                continue  # taken by another drainer between the glob and the read
            for ln in lines:
                rec = json.loads(ln)
                self.add(rec["event"], rec["urls"])  # raises on a database error: the file stays for next time
                moved += 1
            path.unlink()
        return moved

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
        try:
            self.outbox.ingest_spill()
        except sqlite3.Error:
            pass  # still contended; the draining file is kept and retried on the next pass
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
                               headers=signed_headers(self.secret, "POST", url, body, delivery_id=eid, event=etype))
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
                                                    "base_ref", "allowed_paths", "requester", "source", "source_ref",
                                                    "agent_id")},
                "budget": d.get("budget"), "approval_expires_at": d.get("approval_expires_at")}
    elif etype == "approval.decided":
        data = {"decision": "approve" if ev.event == "approved" else "reject", "approver": ev.actor,
                "note": d.get("note", ""), "conditions": d.get("conditions", []),
                "restrictions": d.get("restrictions")}
    elif etype == "approval.expired":
        job = _load_job(cfg, ev.job_id)
        wi = job.work_item if job else None
        data = {"expired_at": d.get("expires_at"), "title": wi.title if wi else None,
                "requester": wi.requester if wi else None, "agent_id": d.get("agent_id"),
                "source": wi.source if wi else None, "source_ref": wi.source_ref if wi else None}
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
        data = {"status": d.get("status"), "error": d.get("error"), "audit_tail_hash": ev.hash,
                "agent_id": job.work_item.agent_id if job else None}
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
    elif etype == "job.published":
        data = {"step": ev.event, "actor": ev.actor, "branch": d.get("branch"), "remote": d.get("remote"),
                "commit": d.get("commit"), "url": d.get("url"), "base": d.get("base"), "draft": d.get("draft")}
    elif etype == "control.kill_switch":
        data = {"enabled": d.get("enabled"), "reason": d.get("reason", ""), "actor": ev.actor,
                "scope": d.get("scope", "global"), "agent_id": d.get("agent_id")}
    else:  # job.cancel_requested
        data = {"by": ev.actor, "scope": d.get("scope", "job"), "reason": d.get("reason", "")}
    event = {"id": f"evt_{uuid.uuid4().hex}", "type": etype, "created_at": now_iso(), "source": cfg.webhooks.source_name,
             "job_id": ev.job_id if not ev.job_id.startswith("_") else None, "audit_seq": ev.seq, "data": data}
    return redact_event(cfg, event)


def redact_event(cfg: Config, event: dict[str, Any]) -> dict[str, Any]:
    """Never let a planted secret leave via a webhook, whichever event carries it."""
    if not cfg.policy.canary_strings:
        return event
    text = json.dumps(event, default=str, ensure_ascii=False)
    for c in cfg.policy.canary_strings:
        if c:
            text = text.replace(c, "[REDACTED]")
    return json.loads(text)


def approval_requested_event(cfg: Config, job: Job) -> dict[str, Any]:
    wi = job.work_item
    return redact_event(cfg, {
        "id": f"evt_{uuid.uuid4().hex}", "type": "approval.requested", "created_at": now_iso(),
        "source": cfg.webhooks.source_name, "job_id": job.id, "audit_seq": None,
        "data": {"title": wi.title, "description": wi.description[:4000], "acceptance_criteria": wi.acceptance_criteria,
                 "requester": wi.requester, "repo_path": wi.repo_path, "allowed_paths": wi.allowed_paths,
                 "budget": job.budget.model_dump(), "source": wi.source, "source_ref": wi.source_ref,
                 "agent_id": wi.agent_id, "approval_expires_at": job.approval_expires_at,
                 "decide": {"method": "POST", "path": f"/v1/jobs/{job.id}/decision",
                            "body": {"decision": "approve|reject", "approver": "<user>", "note": "", "conditions": [],
                                     "restrictions": {"allowed_paths": None, "allowed_commands": None,
                                                      "budget": {}}}}}})


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
        self.outbox.add_or_spill(event, self.cfg.webhooks.outbound_urls)
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
