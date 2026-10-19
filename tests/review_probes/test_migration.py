"""Schema migration probes, from the external reviewer's fourth pass (19 October 2026): a pre-0.1.1 database
keeps its control metadata through the `delivery_id` migration, fresh databases carry the column, and two
constructors racing the migration both succeed."""

from __future__ import annotations

import sqlite3
import threading
from concurrent.futures import ThreadPoolExecutor

import pytest

import gca.queue as queue_module
from gca.queue import JobQueue


def legacy_db(path, value="on"):
    conn = sqlite3.connect(path)
    try:
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("CREATE TABLE controls (name TEXT PRIMARY KEY, value TEXT NOT NULL, "
                     "reason TEXT, set_by TEXT, set_at TEXT NOT NULL)")
        conn.execute("INSERT INTO controls VALUES (?, ?, ?, ?, ?)",
                     ("kill:global", value, "original reason", "original actor", "2026-10-19T00:00:00+00:00"))
        conn.commit()
    finally:
        conn.close()


@pytest.mark.parametrize("value", ["on", "off"])
def test_legacy_migration_preserves_metadata_and_reopens(tmp_path, value):
    path = tmp_path / "legacy.db"
    legacy_db(path, value)
    expected = dict(value=value, reason="original reason", set_by="original actor",
                    set_at="2026-10-19T00:00:00+00:00", delivery_id=None)
    for _ in range(3):
        q = JobQueue(path)
        try:
            assert q.kill_switch_record() == expected
            assert bool(q.kill_switch()) == (value == "on")
            columns = list(q._conn.execute("PRAGMA table_info(controls)"))
            delivery = [c for c in columns if c[1] == "delivery_id"]
            assert len(delivery) == 1
            assert delivery[0][2] == "TEXT" and delivery[0][3] == 0
        finally:
            q.close()
    q = JobQueue(path)
    try:
        q.set_kill_switch(True, reason="webhook", actor="sender", delivery_id="delivery-1")
        assert q.kill_switch_record()["delivery_id"] == "delivery-1"
        q.set_kill_switch(False, reason="CLI off", actor="operator")
        assert q.kill_switch_record()["delivery_id"] is None
        assert q.kill_switch() is None
    finally:
        q.close()


def test_fresh_schema_and_reopen(tmp_path):
    path = tmp_path / "fresh.db"
    for _ in range(2):
        q = JobQueue(path)
        try:
            assert q.kill_switch_record() is None
            assert [r[1] for r in q._conn.execute("PRAGMA table_info(controls)")].count("delivery_id") == 1
        finally:
            q.close()


@pytest.mark.parametrize("existing", [False, True])
def test_concurrent_first_opens_both_succeed(tmp_path, monkeypatch, existing):
    """Both constructors are made to observe the pre-migration schema before either alters it."""
    path = tmp_path / "concurrent.db"
    if existing:
        legacy_db(path)
    else:
        conn = sqlite3.connect(path)
        conn.execute("PRAGMA journal_mode=WAL")
        conn.close()
    original_open = queue_module.open_db
    both_read_old_schema = threading.Barrier(2)

    class ConnectionProxy:
        def __init__(self, connection):
            self.connection = connection

        def execute(self, statement, *args):
            cursor = self.connection.execute(statement, *args)
            if statement == "PRAGMA table_info(controls)":
                rows = cursor.fetchall()
                both_read_old_schema.wait(timeout=5)  # scheduling only: each sees the real pre-migration schema
                return rows
            return cursor

        def cursor(self):
            return self.connection.cursor()

        def close(self):
            self.connection.close()

    monkeypatch.setattr(queue_module, "open_db",
                        lambda owner, db_path: ConnectionProxy(original_open(owner, db_path)))

    def open_queue():
        try:
            q = JobQueue(path)
        except Exception as exc:  # noqa: BLE001
            return f"{type(exc).__name__}: {exc}"
        q.close()
        return "ok"

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _: open_queue(), range(2)))
    assert results == ["ok", "ok"]
