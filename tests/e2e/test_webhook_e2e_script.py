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
