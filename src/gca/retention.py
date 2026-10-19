"""Retention: what gca keeps, for how long, and the one command that prunes it.

Three stores grow without bound unless pruned:

- `jobs` rows (queue). A terminal job's row holds its work item, result and report. Kept `jobs_days` after
  its last update, then deleted. The job's audit log is **not** touched: it is the evidence and is kept
  until the operator archives it (`docs/runbook.md`).
- `webhook_outbox` rows. Delivered rows are kept `outbox_days` for `gca webhook-status`; dead rows are kept
  the same period so they can be requeued, then dropped (the event is still in the audit log).
- `inbound_deliveries` rows (idempotency records). Kept `inbound_days`; this window must exceed the
  platform's longest retry horizon, or a very late retry would be executed again.

Nothing here deletes files: audit logs, eval results and git branches (`gca/<job id>`) stay. Pruning is a
single short transaction per table and is safe to run while the worker and the inbound server are up.
"""

from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

from .config import Config
from .models import TERMINAL_STATES


def _cutoff(days: int) -> str:
    return (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()


def _table_exists(conn: sqlite3.Connection, name: str) -> bool:
    return conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)).fetchone() is not None


def prune(cfg: Config, *, dry_run: bool = False) -> dict[str, int]:
    """Apply `[retention]` to the job store. Returns the number of rows removed (or that would be) per table."""
    r = cfg.retention
    out = {"jobs": 0, "webhook_outbox": 0, "inbound_deliveries": 0}
    if not Path(cfg.db_path).exists():
        return out
    conn = sqlite3.connect(str(cfg.db_path), isolation_level=None)
    try:
        conn.execute("PRAGMA busy_timeout=5000")
        terminal = tuple(s.value for s in TERMINAL_STATES)
        plans = [
            ("jobs", f"status IN ({','.join('?' * len(terminal))}) AND updated_at < ?", (*terminal, _cutoff(r.jobs_days))),
            ("webhook_outbox", "status IN ('delivered', 'dead') AND created_at < ?", (_cutoff(r.outbox_days),)),
            # Age by the immutable first-received time, never by `received_at`: that column is the lease, and a
            # released reservation carries a 1970 lease so a retry can take it over at once. Pruning on the lease
            # would drop a fresh delivery's history and let a stale kill-switch "off" be re-applied later.
            ("inbound_deliveries", "COALESCE(first_received_at, received_at) < ?", (_cutoff(r.inbound_days),)),
        ]
        for table, where, params in plans:
            if not _table_exists(conn, table):
                continue
            n = conn.execute(f"SELECT COUNT(*) FROM {table} WHERE {where}", params).fetchone()[0]
            out[table] = int(n)
            if n and not dry_run:
                conn.execute("BEGIN IMMEDIATE")
                try:
                    conn.execute(f"DELETE FROM {table} WHERE {where}", params)
                    conn.execute("COMMIT")
                except Exception:
                    conn.execute("ROLLBACK")
                    raise
        if not dry_run and any(out.values()):
            conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    finally:
        conn.close()
    return out
