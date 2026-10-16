"""Append-only, hash-chained audit log: one JSONL file per job.

Each event's hash covers the previous hash, so any edit or deletion in the middle of the
log is detectable with `verify_chain`.
"""

from __future__ import annotations

import hashlib
import json
import threading
from pathlib import Path
from typing import Any

from .models import AuditEvent, now_iso

GENESIS = "0" * 64


def _hash(prev_hash: str, payload: dict[str, Any]) -> str:
    body = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256((prev_hash + body).encode("utf-8")).hexdigest()


class AuditLog:
    def __init__(self, audit_dir: Path, job_id: str, redactions: list[str] | None = None):
        self.path = Path(audit_dir) / f"{job_id}.jsonl"
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.job_id = job_id
        self.redactions = [r for r in (redactions or []) if r]
        self._lock = threading.Lock()
        self._seq, self._last_hash = self._tail()

    def _tail(self) -> tuple[int, str]:
        if not self.path.exists():
            return 0, GENESIS
        last: dict[str, Any] | None = None
        with self.path.open("r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line:
                    last = json.loads(line)
        if not last:
            return 0, GENESIS
        return int(last["seq"]), str(last["hash"])

    def _redact(self, data: dict[str, Any]) -> dict[str, Any]:
        if not self.redactions:
            return data
        text = json.dumps(data, default=str, ensure_ascii=False)
        for r in self.redactions:
            text = text.replace(r, "[REDACTED]")
        return json.loads(text)

    def record(self, actor: str, event: str, **data: Any) -> AuditEvent:
        data = self._redact(data)
        with self._lock:
            seq = self._seq + 1
            payload: dict[str, Any] = {"seq": seq, "ts": now_iso(), "job_id": self.job_id, "actor": actor,
                                       "event": event, "data": data}
            h = _hash(self._last_hash, payload)
            ev = AuditEvent(seq=seq, ts=payload["ts"], job_id=self.job_id, actor=actor, event=event, data=data,
                            prev_hash=self._last_hash, hash=h)
            with self.path.open("a", encoding="utf-8") as f:
                f.write(ev.model_dump_json() + "\n")
            self._seq, self._last_hash = seq, h
            return ev

    def events(self) -> list[AuditEvent]:
        if not self.path.exists():
            return []
        out: list[AuditEvent] = []
        with self.path.open("r", encoding="utf-8") as f:
            for line in f:
                if line.strip():
                    out.append(AuditEvent.model_validate_json(line))
        return out


def verify_chain(path: Path) -> tuple[bool, str]:
    """Return (ok, message). Detects tampering, reordering and deletion."""
    prev = GENESIS
    expected_seq = 1
    with Path(path).open("r", encoding="utf-8") as f:
        for n, line in enumerate(f, 1):
            if not line.strip():
                continue
            ev = json.loads(line)
            if ev["seq"] != expected_seq:
                return False, f"line {n}: seq {ev['seq']} != expected {expected_seq}"
            if ev["prev_hash"] != prev:
                return False, f"line {n}: prev_hash mismatch"
            payload = {k: ev[k] for k in ("seq", "ts", "job_id", "actor", "event", "data")}
            if _hash(prev, payload) != ev["hash"]:
                return False, f"line {n}: hash mismatch (content altered)"
            prev = ev["hash"]
            expected_seq += 1
    return True, f"ok ({expected_seq - 1} events)"
