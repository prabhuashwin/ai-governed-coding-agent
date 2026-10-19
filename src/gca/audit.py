"""Append-only, hash-chained audit log: one JSONL file per job.

Each event's hash covers the previous hash, so any edit or deletion in the middle of the
log is detectable with `verify_chain`.
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import os
import sys
import threading
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import IO, Any

from .models import AuditEvent, now_iso

GENESIS = "0" * 64

if sys.platform == "win32":  # pragma: no cover - exercised by the Windows CI job
    import msvcrt

    def _lock(f: IO[bytes]) -> None:
        f.seek(0)
        while True:
            try:
                msvcrt.locking(f.fileno(), msvcrt.LK_LOCK, 1)  # blocks up to ~10 s, then raises
                return
            except OSError:
                continue

    def _unlock(f: IO[bytes]) -> None:
        f.seek(0)
        msvcrt.locking(f.fileno(), msvcrt.LK_UNLCK, 1)
else:
    import fcntl

    def _lock(f: IO[bytes]) -> None:
        fcntl.flock(f.fileno(), fcntl.LOCK_EX)

    def _unlock(f: IO[bytes]) -> None:
        fcntl.flock(f.fileno(), fcntl.LOCK_UN)


@contextlib.contextmanager
def _exclusive(lock_path: Path) -> Iterator[None]:
    """A cross-process exclusive lock (one per audit file), held while an event is appended."""
    with lock_path.open("ab") as f:
        _lock(f)
        try:
            yield
        finally:
            _unlock(f)

# Observers notified after every recorded event (e.g. the webhook outbox). Keyed so re-installing replaces.
_SINKS: dict[str, Callable[[AuditEvent], None]] = {}


def set_sink(name: str, fn: Callable[[AuditEvent], None] | None) -> None:
    if fn is None:
        _SINKS.pop(name, None)
    else:
        _SINKS[name] = fn


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
        """(seq, hash) of the last event on disk: read from the end of the file, so it stays cheap as logs grow."""
        if not self.path.exists():
            return 0, GENESIS
        with self.path.open("rb") as f:
            f.seek(0, os.SEEK_END)
            size = f.tell()
            chunk = b""
            while size > 0:
                step = min(65536, size)
                size -= step
                f.seek(size)
                chunk = f.read(step) + chunk
                if b"\n" in chunk.rstrip(b"\n"):  # a line boundary before the last line: it is complete
                    break
        lines = [ln for ln in chunk.split(b"\n") if ln.strip()]
        if not lines:
            return 0, GENESIS
        last = json.loads(lines[-1].decode("utf-8"))
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
        # Several processes append to one job's log (the worker, `gca cancel`, the inbound server). The chain
        # is only valid if each event follows the one *on disk*, so the tail is re-read under a file lock.
        with self._lock, _exclusive(self.path.with_suffix(".lock")):
            self._seq, self._last_hash = self._tail()
            seq = self._seq + 1
            payload: dict[str, Any] = {"seq": seq, "ts": now_iso(), "job_id": self.job_id, "actor": actor,
                                       "event": event, "data": data}
            h = _hash(self._last_hash, payload)
            ev = AuditEvent(seq=seq, ts=payload["ts"], job_id=self.job_id, actor=actor, event=event, data=data,
                            prev_hash=self._last_hash, hash=h)
            with self.path.open("a", encoding="utf-8") as f:
                f.write(ev.model_dump_json() + "\n")
                f.flush()
            self._seq, self._last_hash = seq, h
        for sink in list(_SINKS.values()):
            try:
                sink(ev)
            except Exception:  # noqa: BLE001 - observers must never break the audit trail
                pass
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
