#!/usr/bin/env python3
"""Cross-process webhook test: gca and a stand-in "Koru" platform talking over real, signed HTTP.

    python scripts/webhook_e2e.py                         # scripted model (free, deterministic)
    python scripts/webhook_e2e.py --provider openai --model gpt-4.1-mini   # real model (needs OPENAI_API_KEY)

Three separate OS processes:
  1. gca            `gca webhook-serve --with-worker`  (approval.mode = webhook)
  2. fake Koru      records every signed event; on `approval.requested` it POSTs a signed approval back,
                    exactly as Koru's approval routing would
  3. this driver    submits a work item like a Koru agent skill would, then checks the whole conversation

Checks (the contract an adapter on the platform side can rely on): every event carries a valid signature;
the lifecycle is submitted -> approval.requested -> approval.decided (by the platform's approver, with enforced
restrictions) -> started -> verified -> finished(succeeded); the job status endpoint agrees; an unsigned
command is rejected; a submission outside `allowed_repos` is refused; a replayed delivery is idempotent; the
platform can publish the verified commit (`job.published`); the kill switch round-trips at instance scope and
stops one job at job scope. Writes webhook-e2e.json/.md and webhook-e2e.contract.json (one sample per event
type and per command: request and response), which is what the adapter team codes against.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
from gca.webhooks import signed_headers, verify_signature  # noqa: E402

# generated once by the driver and handed to the child processes via the environment
OUT_SECRET = os.environ.setdefault("E2E_OUT_SECRET", "e2e-outbound-" + uuid.uuid4().hex[:8])
IN_SECRET = os.environ.setdefault("E2E_IN_SECRET", "e2e-inbound-" + uuid.uuid4().hex[:8])


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


# ----------------------------------------------------------------------------- fake Koru (process 2)
def run_receiver(port: int, out_file: Path, gca_url: str, approver: str) -> None:
    class H(BaseHTTPRequestHandler):
        def do_POST(self):  # noqa: N802
            body = self.rfile.read(int(self.headers.get("Content-Length") or 0))
            reason = verify_signature(OUT_SECRET, "POST", self.path, dict(self.headers), body)
            record = {"valid_signature": reason is None, "reason": reason, "headers_event": self.headers.get("X-GCA-Event")}
            if reason is None:
                record["event"] = json.loads(body)
            with out_file.open("a", encoding="utf-8") as f:
                f.write(json.dumps(record) + "\n")
            self.send_response(204 if reason is None else 401)
            self.end_headers()
            ev = record.get("event") or {}
            if ev.get("type") == "approval.requested":  # Koru's approval routing decides and calls back
                decide = json.dumps({"decision": "approve", "approver": approver, "note": "approved by e2e policy",
                                     "conditions": ["touch only stats.py"],
                                     "restrictions": {"allowed_paths": ["stats.py"], "budget": {"max_turns": 12}}}).encode()
                path = ev["data"]["decide"]["path"]
                h = signed_headers(IN_SECRET, "POST", path, decide, delivery_id=f"koru-{uuid.uuid4().hex}")
                try:
                    dr = httpx.post(gca_url + path, content=decide, headers=h, timeout=10)
                    with out_file.open("a", encoding="utf-8") as f:
                        f.write(json.dumps({"decision": {"request": {"method": "POST", "path": path,
                                                                     "body": json.loads(decide)},
                                                         "response": {"status": dr.status_code, "body": dr.json()}}}) + "\n")
                except httpx.HTTPError as e:  # recorded, the driver will notice the job never runs
                    with out_file.open("a", encoding="utf-8") as f:
                        f.write(json.dumps({"callback_error": str(e)}) + "\n")

        def log_message(self, *a):
            pass

    ThreadingHTTPServer(("127.0.0.1", port), H).serve_forever()


# ----------------------------------------------------------------------------- driver (process 3)
def wait_http(url: str, timeout: float = 30) -> None:
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        try:
            if httpx.get(url, timeout=2).status_code < 500:
                return
        except httpx.HTTPError:
            pass
        time.sleep(0.2)
    raise RuntimeError(f"{url} did not come up")


def read_events(out_file: Path) -> list[dict]:
    if not out_file.exists():
        return []
    return [json.loads(line) for line in out_file.read_text(encoding="utf-8").splitlines() if line.strip()]


CONTRACT: dict = {"commands": {}, "events": {}}


def signed_post(client: httpx.Client, url: str, body: dict, *, delivery_id: str | None = None,
                sample: str | None = None) -> httpx.Response:
    raw = json.dumps(body).encode()
    path = "/" + url.split("/", 3)[3] if url.startswith("http") else url
    delivery_id = delivery_id or f"drv-{uuid.uuid4().hex}"
    r = client.post(url, content=raw, headers=signed_headers(IN_SECRET, "POST", path, raw, delivery_id=delivery_id))
    if sample and sample not in CONTRACT["commands"]:
        CONTRACT["commands"][sample] = {"request": {"method": "POST", "path": path, "delivery_id": delivery_id, "body": body},
                                        "response": {"status": r.status_code, "body": _json(r)}}
    return r


def _json(r: httpx.Response):
    try:
        return r.json()
    except ValueError:
        return r.text


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--provider", choices=["scripted", "openai"], default="scripted")
    ap.add_argument("--model", default="gpt-4.1-mini")
    ap.add_argument("--timeout", type=float, default=300)
    ap.add_argument("--out", default="webhook-e2e")
    ap.add_argument("--keep", action="store_true", help="keep the temp directory for inspection")
    ap.add_argument("--receiver", nargs=4, metavar=("PORT", "OUT", "GCA_URL", "APPROVER"), help=argparse.SUPPRESS)
    args = ap.parse_args()
    if args.receiver:
        port, out, url, approver = args.receiver
        run_receiver(int(port), Path(out), url, approver)
        return 0

    tmp = Path(tempfile.mkdtemp(prefix="gca-webhook-e2e-"))
    repo = tmp / "repo"
    shutil.copytree(ROOT / "evals" / "tasks" / "01-off-by-one-mean" / "repo", repo,
                    ignore=shutil.ignore_patterns("__pycache__", ".pytest_cache"))
    genv = {**os.environ, "GIT_AUTHOR_NAME": "e2e", "GIT_AUTHOR_EMAIL": "e2e@localhost",
            "GIT_COMMITTER_NAME": "e2e", "GIT_COMMITTER_EMAIL": "e2e@localhost"}
    for cmd in (["git", "init", "-q", "-b", "main"], ["git", "add", "-A"], ["git", "commit", "-q", "-m", "fixture"]):
        subprocess.run(cmd, cwd=repo, check=True, env=genv, capture_output=True)
    remote = tmp / "remote.git"  # the tenant's git remote: a bare repository, so publishing can be exercised offline
    subprocess.run(["git", "init", "-q", "--bare", str(remote)], check=True, env=genv, capture_output=True)
    for cmd in (["git", "remote", "add", "origin", str(remote)], ["git", "push", "-q", "origin", "main"],
                ["git", "remote", "set-head", "origin", "main"]):
        subprocess.run(cmd, cwd=repo, check=True, env=genv, capture_output=True)
    outside = tmp / "outside"  # a checkout this instance is not allowed to touch
    outside.mkdir()

    gca_port, koru_port = free_port(), free_port()
    gca_url, koru_url = f"http://127.0.0.1:{gca_port}", f"http://127.0.0.1:{koru_port}/hooks/gca"
    events_file = tmp / "koru-events.jsonl"
    py = Path(sys.executable)
    if args.provider == "openai":
        if not os.environ.get("OPENAI_API_KEY"):
            print("OPENAI_API_KEY is not set", file=sys.stderr)
            return 2
        provider = (f'kind = "openai_compatible"\nmodel = "{args.model}"\napi_key_env = "OPENAI_API_KEY"\n'
                    f'max_tokens = 4096\n')
    else:
        provider = f'kind = "scripted"\nscript_path = "{(ROOT / "scripts" / "smoke_script.json").as_posix()}"\n'
    cfg = tmp / "gca.toml"
    cfg.write_text(
        f'data_dir = "{(tmp / "gca-data").as_posix()}"\nworker_id = "e2e-worker"\n'
        f"[provider]\n{provider}"
        f'[budget]\nmax_turns = 15\nmax_cost_usd = 0.25\nmax_seconds = 300\n'
        f'[policy]\nallowed_commands = ["python", "python3", "{py.name}"]\nallowed_repos = ["{repo.as_posix()}"]\n'
        f'[approval]\nmode = "webhook"\n'
        f'[webhooks]\nenabled = true\noutbound_urls = ["{koru_url}"]\ninbound_port = {gca_port}\n'
        f'[pricing]\ninput_per_million = {{ "gpt-4.1-mini" = 0.4, "gpt-4.1" = 2.0 }}\n'
        f'output_per_million = {{ "gpt-4.1-mini" = 1.6, "gpt-4.1" = 8.0 }}\n')
    env = {**os.environ, "GCA_WEBHOOK_SECRET": OUT_SECRET, "GCA_INBOUND_SECRET": IN_SECRET,
           "PATH": f"{py.parent}{os.pathsep}{os.environ['PATH']}"}
    logs = {n: (tmp / f"{n}.log").open("wb") for n in ("gca", "koru")}
    koru = subprocess.Popen([sys.executable, __file__, "--receiver", str(koru_port), str(events_file), gca_url,
                             "koru-approver"], stdout=logs["koru"], stderr=subprocess.STDOUT, env=env)
    gca = subprocess.Popen([sys.executable, "-m", "gca.cli", "-c", str(cfg), "webhook-serve", "--with-worker",
                            "--poll", "0.5", "--flush-every", "1"], stdout=logs["gca"], stderr=subprocess.STDOUT, env=env)
    checks: dict[str, bool] = {}
    result: dict = {"provider": args.provider, "model": args.model if args.provider == "openai" else "scripted"}
    try:
        wait_http(f"{gca_url}/healthz")
        with httpx.Client(timeout=15) as c:
            checks["unsigned_command_rejected"] = c.post(f"{gca_url}/v1/work-items", content=b"{}").status_code == 401
            work_item = {
                "title": "Fix mean() off-by-one", "requester": "alice@corp", "source": "koru", "source_ref": "KORU-REQ-1",
                "description": "stats.mean([2, 4, 6]) returns 6.0 instead of 4.0. Fix it.",
                "acceptance_criteria": ["mean() returns the arithmetic mean", "mean([]) still raises ValueError",
                                        "all tests pass"],
                "repo_path": str(repo), "test_command": [str(py), "-m", "pytest", "-q", "-p", "no:cacheprovider"],
                "idempotency_key": "koru:KORU-REQ-1"}
            submit_delivery = f"drv-{uuid.uuid4().hex}"
            r = signed_post(c, f"{gca_url}/v1/work-items", work_item, delivery_id=submit_delivery, sample="submit")
            checks["submit_accepted"] = r.status_code == 201
            job_id = r.json().get("job_id")
            result["job_id"] = job_id
            rr = signed_post(c, f"{gca_url}/v1/work-items", work_item, delivery_id=submit_delivery, sample="submit_replayed")
            checks["replayed_delivery_is_idempotent"] = rr.status_code == 201 and rr.json() == {**r.json(), "replayed": True}
            r2 = signed_post(c, f"{gca_url}/v1/work-items", {
                "title": "x", "requester": "mallory@corp", "description": "d", "acceptance_criteria": [],
                "repo_path": str(outside), "test_command": ["pytest"]}, sample="submit_outside_allowed_repos")
            checks["repo_outside_allow_list_refused"] = r2.status_code == 403

            end = time.monotonic() + args.timeout
            while time.monotonic() < end:
                evs = [e.get("event", {}) for e in read_events(events_file)]
                if any(e.get("type") == "job.finished" and e.get("job_id") == job_id for e in evs):
                    break
                time.sleep(1)
            records = read_events(events_file)
            evs = [rec["event"] for rec in records if rec.get("event")]
            types = [e["type"] for e in evs if e.get("job_id") == job_id]
            result["event_types"] = types
            checks["all_signatures_valid"] = bool(records) and all(rec.get("valid_signature", True) for rec in records)
            checks["no_callback_errors"] = not any("callback_error" in rec for rec in records)
            expected = ["job.submitted", "approval.requested", "approval.decided", "job.started", "job.verified",
                        "job.finished"]
            checks["lifecycle_in_order"] = [t for t in types if t in expected] == expected
            decided = next((e for e in evs if e["type"] == "approval.decided"), {})
            checks["approved_by_platform"] = decided.get("data", {}).get("approver") == "koru-approver"
            checks["restrictions_enforced"] = decided.get("data", {}).get("restrictions") == {
                "allowed_paths": ["stats.py"], "budget": {"max_turns": 12}}
            dec = next((rec["decision"] for rec in records if rec.get("decision")), None)
            if dec:
                CONTRACT["commands"]["decision"] = dec
                checks["decision_echoes_enforced"] = dec["response"]["body"].get("enforced", {}).get("allowed_paths") == ["stats.py"]
            fin = next((e for e in evs if e["type"] == "job.finished" and e.get("job_id") == job_id), {})
            fd = fin.get("data", {})
            result["finished"] = {k: fd.get(k) for k in ("status", "error", "branch", "model")}
            result["usage"] = (fd.get("agent") or {}).get("usage")
            checks["job_succeeded"] = fd.get("status") == "succeeded"
            checks["verified_independently"] = bool((fd.get("verification") or {}).get("passed"))
            checks["audit_hash_present"] = len(fd.get("audit_tail_hash", "")) == 64

            hd = signed_headers(IN_SECRET, "GET", f"/v1/jobs/{job_id}", b"")
            st = c.get(f"{gca_url}/v1/jobs/{job_id}", headers=hd).json()
            checks["status_endpoint_agrees"] = st.get("status") == fd.get("status") and st.get("approver") == "koru-approver"
            on = signed_post(c, f"{gca_url}/v1/controls/kill-switch", {"enabled": True, "actor": "secops", "reason": "drill"})
            killed = c.get(f"{gca_url}/healthz").json().get("kill_switch") is True
            off = signed_post(c, f"{gca_url}/v1/controls/kill-switch",
                              {"enabled": False, "actor": "secops", "reason": "drill over"})
            checks["kill_switch_round_trip"] = (on.status_code == 200 and killed and off.status_code == 200
                                                and c.get(f"{gca_url}/healthz").json().get("kill_switch") is False)
            CONTRACT["commands"]["kill_switch_on"] = {"request": {"method": "POST", "path": "/v1/controls/kill-switch",
                                                                  "body": {"enabled": True, "actor": "secops", "reason": "drill", "scope": "global"}},
                                                      "response": {"status": on.status_code, "body": _json(on)}}

            # the platform publishes the verified commit to the tenant's remote (PR creation needs GitHub; push only here)
            pub = signed_post(c, f"{gca_url}/v1/jobs/{job_id}/publish",
                              {"actor": "alice@corp", "draft": True, "create_pr": False}, sample="publish")
            ls = subprocess.run(["git", "ls-remote", "--heads", "origin"], cwd=repo, capture_output=True, text=True, env=genv)
            checks["publish_pushed_verified_commit"] = (pub.status_code == 200 and pub.json().get("branch") in ls.stdout
                                                        and fd.get("branch") == pub.json().get("branch"))
            end = time.monotonic() + 30
            while time.monotonic() < end and not any(e.get("type") == "job.published" for e in
                                                      (rec.get("event", {}) for rec in read_events(events_file))):
                time.sleep(0.5)
            records = read_events(events_file)
            evs = [rec["event"] for rec in records if rec.get("event")]
            published = next((e for e in evs if e["type"] == "job.published"), {})
            checks["published_event"] = published.get("data", {}).get("step") == "branch_pushed" and \
                published.get("data", {}).get("actor") == "alice@corp"

            # job-scope stop: a second request is stopped by the platform while it is queued or running
            r3 = signed_post(c, f"{gca_url}/v1/work-items", {**work_item, "source_ref": "KORU-REQ-2",
                                                              "idempotency_key": "koru:KORU-REQ-2"})
            job2 = r3.json().get("job_id")
            stop = signed_post(c, f"{gca_url}/v1/controls/kill-switch",
                               {"enabled": True, "actor": "secops", "reason": "wrong ticket", "scope": "job", "job_id": job2},
                               sample="kill_switch_job_scope")
            end = time.monotonic() + 60
            st2 = {}
            while time.monotonic() < end:
                st2 = c.get(f"{gca_url}/v1/jobs/{job2}", headers=signed_headers(IN_SECRET, "GET", f"/v1/jobs/{job2}", b"")).json()
                if st2.get("status") in ("cancelled", "succeeded", "failed"):
                    break
                time.sleep(0.5)
            checks["job_scope_stop_cancels_job"] = stop.status_code == 200 and st2.get("status") == "cancelled"
            records = read_events(events_file)
            evs = [rec["event"] for rec in records if rec.get("event")]
            cr = next((e for e in evs if e["type"] == "job.cancel_requested" and e.get("job_id") == job2), {})
            checks["job_scope_stop_event"] = cr.get("data", {}).get("scope") == "job"
            checks["all_signatures_valid"] = checks["all_signatures_valid"] and all(rec.get("valid_signature", True) for rec in records)
            CONTRACT["commands"]["status"] = {"request": {"method": "GET", "path": f"/v1/jobs/{job_id}", "body": None},
                                              "response": {"status": 200, "body": st}}
            for e in evs:  # one sample per event type, first occurrence
                CONTRACT["events"].setdefault(e["type"], e)
            CONTRACT["signature"] = {"version": 2, "headers": ["X-GCA-Signature", "X-GCA-Signature-Version",
                                                               "X-GCA-Timestamp", "X-GCA-Delivery-Id", "X-GCA-Event"],
                                     "note": "see docs/webhooks.md § Signing"}
    finally:
        for p in (gca, koru):
            p.terminate()
        for p in (gca, koru):
            try:
                p.wait(timeout=10)
            except subprocess.TimeoutExpired:
                p.kill()
        for f in logs.values():
            f.close()
    result["checks"] = checks
    result["passed"] = bool(checks) and all(checks.values())
    out = Path(args.out)
    out.with_suffix(".json").write_text(json.dumps(result, indent=1), encoding="utf-8")
    out.with_suffix(".contract.json").write_text(json.dumps(CONTRACT, indent=1, default=str), encoding="utf-8")
    lines = [f"# Webhook E2E — {'PASS' if result['passed'] else 'FAIL'} ({result['provider']}/{result['model']})", "",
             "| check | result |", "|---|---|"] + [f"| {k} | {'ok' if v else 'FAIL'} |" for k, v in checks.items()]
    lines += ["", f"Events seen by the platform: {', '.join(result.get('event_types', []))}",
              f"Finished: {result.get('finished')}", f"Usage: {result.get('usage')}"]
    out.with_suffix(".md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    print("\n".join(lines))
    if not result["passed"]:
        for n in ("gca", "koru"):
            print(f"--- {n}.log ---\n" + (tmp / f"{n}.log").read_text(errors="replace")[-3000:])
    if not args.keep:
        shutil.rmtree(tmp, ignore_errors=True)
    else:
        print(f"kept {tmp}")
    return 0 if result["passed"] else 1


if __name__ == "__main__":
    sys.exit(main())
