"""Run scripts/webhook_e2e.py (gca + fake Koru + driver as three processes) in scripted mode on every CI run."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

pytestmark = pytest.mark.e2e
SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "webhook_e2e.py"


def test_cross_process_webhook_conversation(tmp_path):
    out = tmp_path / "result"
    cp = subprocess.run([sys.executable, str(SCRIPT), "--timeout", "120", "--out", str(out)],
                        capture_output=True, text=True, timeout=240)
    assert cp.returncode == 0, cp.stdout[-4000:] + cp.stderr[-2000:]
    res = json.loads(out.with_suffix(".json").read_text())
    assert res["passed"] and all(res["checks"].values())
    assert res["event_types"][-1] == "job.finished" and res["finished"]["status"] == "succeeded"
    # the contract samples the adapter team codes against cover every command and every event type gca emits
    contract = json.loads(out.with_suffix(".contract.json").read_text())
    assert set(contract["commands"]) >= {"submit", "submit_replayed", "submit_outside_allowed_repos", "decision",
                                         "publish", "kill_switch_on", "kill_switch_job_scope", "status",
                                         "kill_switch_agent_scope", "submit_with_approval_timeout",
                                         "decision_after_expiry", "submit_email_identity_refused"}
    from gca.webhooks import EVENT_TYPES
    assert set(contract["events"]) == set(EVENT_TYPES) - {"job.blocked_action"}  # no hostile step in the smoke script
    committed = json.loads((SCRIPT.parents[1] / "docs" / "webhook-contract-samples.json").read_text())
    assert set(committed["commands"]) == set(contract["commands"]) and set(committed["events"]) == set(contract["events"])
    for t, ev in contract["events"].items():  # same shape as the committed snapshot: an adapter built on it stays valid
        assert set(ev["data"]) == set(committed["events"][t]["data"]), t
