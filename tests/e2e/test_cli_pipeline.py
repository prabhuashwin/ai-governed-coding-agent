"""End-to-end through the real CLI: init -> submit -> approve -> run -> status/report/audit.

Uses the scripted provider selected purely through gca.toml, exactly as a real deployment
would select Anthropic or an OpenAI-compatible server.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest
from conftest import steps_fix_calc

from gca.cli import main

pytestmark = pytest.mark.e2e


def write_config(tmp_path: Path, script: Path) -> Path:
    cfg = tmp_path / "gca.toml"
    py = Path(sys.executable).name
    cfg.write_text(
        f'data_dir = "{(tmp_path / ".gca").as_posix()}"\n'
        f'[provider]\nkind = "scripted"\nscript_path = "{script.as_posix()}"\n'
        f'[budget]\nmax_turns = 10\nmax_cost_usd = 1.0\n'
        f'[policy]\nallowed_commands = ["python", "{py}"]\n'
        f'[pricing]\ninput_per_million = {{ "scripted" = 1.0 }}\noutput_per_million = {{ "scripted" = 2.0 }}\n'
    )
    return cfg


def test_full_pipeline_via_cli(tmp_path: Path, calc_repo: Path, capsys):
    script = tmp_path / "script.json"
    script.write_text(json.dumps({"steps": steps_fix_calc()}))
    cfg = write_config(tmp_path, script)
    test_cmd = f"{sys.executable} -m pytest -q -p no:cacheprovider"

    assert main(["-c", str(cfg), "submit", "--title", "Fix mean()", "--description", "off by one",
                 "--criteria", "mean divides by n", "--criteria", "tests pass", "--repo", str(calc_repo),
                 "--test-cmd", test_cmd, "--requester", "ashwin", "--idempotency-key", "k1"]) == 0
    job_id = capsys.readouterr().out.strip()
    assert job_id.startswith("job_")

    # idempotent re-submit returns the same id
    main(["-c", str(cfg), "submit", "--title", "Fix mean()", "--description", "off by one", "--repo", str(calc_repo),
          "--test-cmd", test_cmd, "--idempotency-key", "k1"])
    assert capsys.readouterr().out.strip() == job_id

    # running before approval does nothing
    assert main(["-c", str(cfg), "run", "--once"]) == 0
    assert "no approved jobs" in capsys.readouterr().out

    assert main(["-c", str(cfg), "approve", job_id, "--approver", "lead"]) == 0
    assert main(["-c", str(cfg), "run", "--once"]) == 0
    assert "succeeded" in capsys.readouterr().out

    main(["-c", str(cfg), "status", job_id])
    status = json.loads(capsys.readouterr().out)
    assert status["status"] == "succeeded" and status["approval"]["approver"] == "lead"
    assert status["result"]["verification"]["passed"] and status["result"]["branch"] == f"gca/{job_id}"
    assert status["result"]["agent"]["usage"]["cost_usd"] > 0

    main(["-c", str(cfg), "report", job_id, "--diff"])
    report = capsys.readouterr().out
    assert "Independent verification: **PASSED**" in report and "-    return sum(values) / (len(values) - 1)" in report

    assert main(["-c", str(cfg), "audit", job_id, "--verify"]) == 0
    assert capsys.readouterr().out.startswith("OK")
    main(["-c", str(cfg), "audit", job_id])
    lines = capsys.readouterr().out.splitlines()
    actors = {ln.split()[2] for ln in lines}
    assert {"ashwin", "lead", "worker:worker-1", "agent", "verifier"} <= actors

    # the branch exists in the engineer's repo and the working copy is untouched
    from conftest import git
    assert f"gca/{job_id}" in git(calc_repo, "branch", "--list")
    assert "(len(values) - 1)" in (calc_repo / "calc.py").read_text()
    assert "(len(values) - 1)" not in git(calc_repo, "show", f"gca/{job_id}:calc.py")


def test_reject_and_cancel_paths(tmp_path: Path, calc_repo: Path, capsys):
    script = tmp_path / "script.json"
    script.write_text(json.dumps({"steps": []}))
    cfg = write_config(tmp_path, script)
    main(["-c", str(cfg), "submit", "--title", "t", "--description", "d", "--repo", str(calc_repo)])
    a = capsys.readouterr().out.strip()
    assert main(["-c", str(cfg), "approve", a, "--approver", "lead", "--reject", "--note", "out of scope"]) == 0
    assert "rejected" in capsys.readouterr().out
    main(["-c", str(cfg), "submit", "--title", "t2", "--description", "d", "--repo", str(calc_repo)])
    b = capsys.readouterr().out.strip()
    assert main(["-c", str(cfg), "cancel", b]) == 0
    assert "cancelled" in capsys.readouterr().out
    main(["-c", str(cfg), "status"])
    out = capsys.readouterr().out
    assert "rejected" in out and "cancelled" in out


def test_init_writes_example_config(tmp_path: Path, capsys):
    p = tmp_path / "gca.toml"
    assert main(["init", str(p)]) == 0
    assert "[provider]" in p.read_text()
    assert main(["init", str(p)]) == 1  # refuses to overwrite
