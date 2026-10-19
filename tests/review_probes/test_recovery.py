"""Kill-switch delivery recovery probes, from the external reviewer's fourth pass (19 October 2026).

These reproduce the exact scenarios reported as findings so later changes cannot regress them silently:
a crashed-after-effect delivery retried after a newer control change, a second takeover after a crashed
takeover, a concurrent control write racing the supersession guard, and an audit failure after the change.
All databases and secrets are fixtures. A simulated clock drives the reservation TTL.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

import pytest

from gca import inbound, models
from gca.config import Config
from gca.inbound import CommandHandler
from gca.queue import JobQueue
from gca.webhooks import signed_headers

PATH = "/v1/controls/kill-switch"
SECRET = "local-fixture-only"


@pytest.fixture
def setup(tmp_path, monkeypatch):
    class Clock(datetime):
        current = datetime.now(timezone.utc) - timedelta(seconds=242)

        @classmethod
        def now(cls, tz=None):  # type: ignore[override]
            return cls.current if tz else cls.current.replace(tzinfo=None)

    monkeypatch.setenv("GCA_INBOUND_SECRET", SECRET)
    monkeypatch.setattr(models, "datetime", Clock)
    monkeypatch.setattr(inbound, "datetime", Clock)
    cfg = Config(data_dir=str(tmp_path / "state"))
    q = JobQueue(cfg.db_path)
    h = CommandHandler(cfg, q)
    yield cfg, q, h, Clock
    q.close()
    h._conn.close()


def request(enabled, delivery, clock):
    body = json.dumps({"enabled": enabled, "actor": "fixture", "reason": delivery}).encode()
    headers = signed_headers(SECRET, "POST", PATH, body, delivery_id=delivery,
                             timestamp=int(clock.current.timestamp()))
    return headers, body


def apply(h, req):
    return h.handle("POST", PATH, req[0], req[1])


def crash_before(*args):
    raise SystemExit("simulated process exit before effect")


def crashed_applied_off(h, old, monkeypatch):
    """The OFF is applied, then the process dies before its response is recorded by the generic path."""
    route = h._route

    def crash_after(*args):
        route(*args)
        raise SystemExit("simulated process exit after effect")

    with monkeypatch.context() as patch:
        patch.setattr(h, "_route", crash_after)
        with pytest.raises(SystemExit):
            apply(h, old)


def test_original_single_takeover_retains_newer_control(setup, monkeypatch):
    cfg, q, h, clock = setup
    old = request(False, "old", clock)
    crashed_applied_off(h, old, monkeypatch)
    clock.current += timedelta(seconds=1)
    assert apply(h, request(True, "new", clock))[0] == 200
    clock.current += timedelta(seconds=120)
    status, response = apply(h, old)
    assert status in (200, 409) and q.kill_switch() is not None, (status, response)
    assert response.get("replayed") or response.get("already_applied") or "superseded" in response.get("error", "")


def test_second_takeover_retains_original_receipt_order(setup, monkeypatch):
    cfg, q, h, clock = setup
    old = request(False, "old", clock)
    crashed_applied_off(h, old, monkeypatch)
    clock.current += timedelta(seconds=1)
    assert apply(h, request(True, "new", clock))[0] == 200
    clock.current += timedelta(seconds=120)
    # The original probe expected this retry to re-run the command and crash again. With the delivery outcome
    # recorded in the same transaction as the control change, the retry is replayed instead and never re-runs;
    # either path is acceptable here, the invariant under test is the final state below.
    with monkeypatch.context() as patch:
        patch.setattr(h, "_route", crash_before)
        try:
            apply(h, old)
        except SystemExit:
            pass
    clock.current += timedelta(seconds=121)
    status, response = apply(h, old)
    final = q.kill_switch_record()
    assert final["value"] == "on", f"second takeover repeated a superseded change: {status} {response}"


def test_newer_control_between_guard_and_write_is_preserved(setup, monkeypatch):
    """A concurrent control write from another connection, as close to the retried OFF's own write as the
    transaction allows: it either lands before (then the OFF is superseded) or is blocked until after the OFF's
    commit (then it overwrites). Either way the final state is the newer emergency stop."""
    cfg, q, h, clock = setup
    q.set_kill_switch(True, reason="initial", actor="fixture")
    clock.current += timedelta(seconds=1)
    old = request(False, "old", clock)
    with monkeypatch.context() as patch:
        patch.setattr(h, "_route", crash_before)
        with pytest.raises(SystemExit):
            apply(h, old)
    clock.current += timedelta(seconds=121)
    other = JobQueue(cfg.db_path)
    real_apply = JobQueue.apply_kill_switch_delivery

    def concurrent_then_apply(self, *a, **kw):
        other.set_kill_switch(True, reason="new emergency", actor="operator")
        return real_apply(self, *a, **kw)

    try:
        with monkeypatch.context() as patch:
            patch.setattr(JobQueue, "apply_kill_switch_delivery", concurrent_then_apply)
            status, response = apply(h, old)
        final = q.kill_switch_record()
        assert final["value"] == "on" and final["reason"] == "new emergency", \
            f"stale write overwrote newer control: {status} {response} {final}"
    finally:
        other.close()


def test_retry_after_audit_error_retains_delivery_history(setup, monkeypatch):
    cfg, q, h, clock = setup
    old = request(False, "old", clock)

    def unavailable_audit(*args, **kwargs):
        raise OSError("synthetic temporary audit storage failure")

    with monkeypatch.context() as patch:
        patch.setattr(inbound.AuditLog, "record", unavailable_audit)
        status, response = apply(h, old)
    assert status == 200 and response.get("audit_recorded") is False  # the change is committed and reported honestly
    assert q.kill_switch_record()["delivery_id"] == "old"
    clock.current += timedelta(seconds=1)
    assert apply(h, request(True, "new", clock))[0] == 200
    status, response = apply(h, old)
    final = q.kill_switch_record()
    assert final["value"] == "on", f"error recovery lost the original delivery history: {status} {response}"
    assert response.get("replayed") is True
