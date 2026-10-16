#!/usr/bin/env python3
"""Bootstrap and smoke-test the ServiceNow integration against a real instance (e.g. a PDI).

    export SN_INSTANCE_URL=https://devNNNNN.service-now.com SN_USER=admin SN_PASSWORD=...
    python scripts/servicenow_bootstrap.py create-table            # idempotent: u_gca_work_item + fields
    python scripts/servicenow_bootstrap.py seed --repo /path/to/repo [--test-cmd "python -m pytest -q"]
    python scripts/servicenow_bootstrap.py verify <sys_id> --expect succeeded

The table is a global custom table (fields prefixed u_), so set `field_prefix = "u_"` and
`table = "u_gca_work_item"` in [servicenow]. Table creation uses the Table API on sys_db_object /
sys_dictionary, which needs an admin user. If your instance policy blocks that, create the table in
Studio with the same field names instead; everything else is unchanged.
"""

from __future__ import annotations

import argparse
import json
import os
import sys

import httpx

TABLE = "u_gca_work_item"
FIELDS = [
    # (element, label, type, max_length)
    ("u_number", "Number", "string", 40),
    ("u_short_description", "Short description", "string", 160),
    ("u_description", "Description", "string", 4000),
    ("u_acceptance_criteria", "Acceptance criteria (one per line)", "string", 4000),
    ("u_repo_path", "Repository path (on the worker host)", "string", 512),
    ("u_base_ref", "Base ref", "string", 120),
    ("u_test_command", "Test command", "string", 512),
    ("u_allowed_paths", "Allowed write paths (comma-separated)", "string", 1024),
    ("u_state", "State", "string", 40),
    ("u_approver", "Approver", "string", 120),
    ("u_gca_job_id", "GCA job id", "string", 64),
    ("u_gca_branch", "GCA branch", "string", 200),
    ("u_gca_status", "GCA status", "string", 40),
    ("u_gca_error", "GCA error", "string", 1000),
    ("u_gca_report", "GCA report", "string", 65000),
    ("u_gca_audit_hash", "GCA audit hash", "string", 64),
]


def client() -> httpx.Client:
    base = os.environ["SN_INSTANCE_URL"].rstrip("/")
    return httpx.Client(base_url=base, auth=(os.environ["SN_USER"], os.environ["SN_PASSWORD"]), timeout=60,
                        headers={"Accept": "application/json", "Content-Type": "application/json"})


def get(c: httpx.Client, table: str, query: str, fields: str = "sys_id") -> list[dict]:
    r = c.get(f"/api/now/table/{table}", params={"sysparm_query": query, "sysparm_fields": fields, "sysparm_limit": "50"})
    r.raise_for_status()
    return r.json()["result"]


def post(c: httpx.Client, table: str, body: dict, display: bool = False) -> dict:
    params = {"sysparm_input_display_value": "true"} if display else {}
    r = c.post(f"/api/now/table/{table}", json=body, params=params)
    if r.status_code >= 400:
        raise SystemExit(f"POST {table} failed {r.status_code}: {r.text[:800]}")
    return r.json()["result"]


def cmd_create_table(_args) -> None:
    with client() as c:
        if get(c, "sys_db_object", f"name={TABLE}"):
            print(f"{TABLE} exists")
        else:
            post(c, "sys_db_object", {"name": TABLE, "label": "GCA Work Item", "is_extendable": "false",
                                      "create_access_controls": "true"})
            print(f"created table {TABLE}")
        existing = {r["element"] for r in get(c, "sys_dictionary", f"name={TABLE}^elementISNOTEMPTY", "element")}
        for element, label, typ, maxlen in FIELDS:
            if element in existing:
                continue
            post(c, "sys_dictionary", {"name": TABLE, "element": element, "column_label": label,
                                       "internal_type": typ, "max_length": str(maxlen)}, display=True)
            print(f"  added {element}")
        print("table ready")


def cmd_seed(args) -> None:
    body = {
        "u_number": args.number, "u_short_description": "Fix mean() off-by-one (gca smoke test)",
        "u_description": "stats.mean([2,4,6]) returns 6.0 instead of 4.0. Fix the divisor.",
        "u_acceptance_criteria": "mean() returns the arithmetic mean\nmean([]) still raises ValueError\nall tests pass",
        "u_repo_path": args.repo, "u_base_ref": "HEAD", "u_test_command": args.test_cmd,
        "u_state": "approved", "u_approver": args.approver,
    }
    with client() as c:
        rec = post(c, TABLE, body)
    print(rec["sys_id"])


def cmd_verify(args) -> None:
    with client() as c:
        rows = get(c, TABLE, f"sys_id={args.sys_id}",
                   "sys_id,u_state,u_gca_job_id,u_gca_branch,u_gca_status,u_gca_error,u_gca_audit_hash,u_gca_report")
    if not rows:
        raise SystemExit("record not found")
    rec = rows[0]
    summary = {k: (v[:120] + "…" if isinstance(v, str) and len(v) > 120 else v) for k, v in rec.items()}
    print(json.dumps(summary, indent=2))
    problems = []
    if rec.get("u_state") != args.expect:
        problems.append(f"state is {rec.get('u_state')!r}, expected {args.expect!r}")
    if not rec.get("u_gca_job_id"):
        problems.append("gca_job_id not written back")
    if args.expect == "succeeded":
        if not rec.get("u_gca_branch", "").startswith("gca/"):
            problems.append("gca_branch not written back")
        if "Independent verification" not in rec.get("u_gca_report", ""):
            problems.append("gca_report not written back")
        if len(rec.get("u_gca_audit_hash", "")) != 64:
            problems.append("gca_audit_hash missing")
    if problems:
        raise SystemExit("VERIFY FAILED: " + "; ".join(problems))
    print("VERIFY OK")


def main(argv=None) -> None:
    p = argparse.ArgumentParser()
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("create-table").set_defaults(fn=cmd_create_table)
    s = sub.add_parser("seed")
    s.add_argument("--repo", required=True)
    s.add_argument("--test-cmd", default="python -m pytest -q -p no:cacheprovider")
    s.add_argument("--approver", default="smoke-approver")
    s.add_argument("--number", default="GCA0001")
    s.set_defaults(fn=cmd_seed)
    s = sub.add_parser("verify")
    s.add_argument("sys_id")
    s.add_argument("--expect", default="succeeded")
    s.set_defaults(fn=cmd_verify)
    args = p.parse_args(argv)
    for var in ("SN_INSTANCE_URL", "SN_USER", "SN_PASSWORD"):
        if not os.environ.get(var):
            sys.exit(f"{var} is not set")
    args.fn(args)


if __name__ == "__main__":
    main()
