"""`gca prune` applies [retention] to the job store and never touches audit logs."""

from __future__ import annotations

import json
import sqlite3
from contextlib import closing
from datetime import datetime, timedelta, timezone

from gca import webhooks
from gca.audit import AuditLog
from gca.cli import main as cli_main
from gca.config import WebhookConfig
from gca.models import JobStatus
from gca.queue import JobQueue
from gca.retention import prune


def _age(db, table, col, key_col, key, days):
    old = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
    with closing(sqlite3.connect(db)) as c, c:
        c.execute(f"UPDATE {table} SET {col}=? WHERE {key_col}=?", (old, key))


def test_prune_removes_old_terminal_rows_only(config, work_item, monkeypatch):
    monkeypatch.setenv("GCA_WEBHOOK_SECRET", "s")
    config.webhooks = WebhookConfig(enabled=True, outbound_urls=["https://koru.example/h"])
    webhooks.set_default_transport(__import__("httpx").MockTransport(lambda r: __import__("httpx").Response(204)))
    try:
        webhooks.install(config)
        q = JobQueue(config.db_path)
        old_done = q.submit(work_item)
        q.reject(old_done.id, "lead")  # terminal
        recent_done = q.submit(work_item.model_copy(update={"idempotency_key": "k2"}))
        q.reject(recent_done.id, "lead")
        old_open = q.submit(work_item.model_copy(update={"idempotency_key": "k3"}))  # submitted: never pruned
        AuditLog(config.audit_dir, old_done.id).record("x", "submitted")
        webhooks.flush(config)
        with closing(sqlite3.connect(config.db_path)) as c, c:
            c.execute("INSERT INTO inbound_deliveries (id, received_at, status, response, first_received_at) VALUES "
                      "('old', ?, 201, '{}', ?)", ((datetime.now(timezone.utc) - timedelta(days=30)).isoformat(),) * 2) \
                if c.execute("SELECT 1 FROM sqlite_master WHERE name='inbound_deliveries'").fetchone() else None
        _age(config.db_path, "jobs", "updated_at", "id", old_done.id, 120)
        _age(config.db_path, "jobs", "updated_at", "id", old_open.id, 120)
        with closing(sqlite3.connect(config.db_path)) as c, c:
            c.execute("UPDATE webhook_outbox SET created_at=? WHERE job_id=?",
                      ((datetime.now(timezone.utc) - timedelta(days=30)).isoformat(), old_done.id))
            outbox_before = c.execute("SELECT COUNT(*) FROM webhook_outbox").fetchone()[0]
        q.close()

        dry = prune(config, dry_run=True)
        assert dry["jobs"] == 1 and dry["webhook_outbox"] >= 1
        q = JobQueue(config.db_path)
        assert {j.id for j in q.list()} == {old_done.id, recent_done.id, old_open.id}  # dry run changed nothing
        q.close()

        removed = prune(config)
        assert removed == dry
        q = JobQueue(config.db_path)
        ids = {j.id for j in q.list()}
        assert old_done.id not in ids and recent_done.id in ids and old_open.id in ids
        assert q.get(old_open.id).status == JobStatus.SUBMITTED
        q.close()
        with closing(sqlite3.connect(config.db_path)) as c, c:
            assert c.execute("SELECT COUNT(*) FROM webhook_outbox").fetchone()[0] == outbox_before - removed["webhook_outbox"]
        # the audit log of the pruned job is untouched
        assert AuditLog(config.audit_dir, old_done.id).events()
        assert prune(config) == {"jobs": 0, "webhook_outbox": 0, "inbound_deliveries": 0}
    finally:
        webhooks.set_default_transport(None)
        webhooks.uninstall()


def test_prune_cli_and_missing_db(tmp_path, capsys):
    toml = tmp_path / "gca.toml"
    toml.write_text(f'data_dir = "{(tmp_path / "d").as_posix()}"\n[retention]\njobs_days = 7\n')
    assert cli_main(["-c", str(toml), "prune", "--dry-run"]) == 0  # no database yet: nothing to do
    out = json.loads(capsys.readouterr().out)
    assert out["removed"] == {"jobs": 0, "webhook_outbox": 0, "inbound_deliveries": 0} and out["retention_days"]["jobs_days"] == 7
