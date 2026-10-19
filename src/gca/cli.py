"""`gca` command line: submit -> approve -> run -> report, plus audit and eval."""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

from . import __version__
from .audit import AuditLog, verify_chain
from .cmdline import split_command
from .config import Config, load_config
from .models import JobStatus, Restrictions, WorkItem
from .policy import PolicyViolation, check_repo_allowed
from .queue import JobQueue
from .worker import Worker

EXAMPLE_CONFIG = """# gca.toml — governed coding agent configuration
data_dir = ".gca"
worker_id = "worker-1"

[workspace]
# kind: worktree (git worktree on this host) | container (each command in a throwaway docker/podman container)
kind = "worktree"
runtime = "docker"
image = "python:3.12-slim"   # must contain the toolchain your test command needs
network = "none"
memory = "2g"
cpus = "2"

[provider]
# kind: anthropic | openai_compatible | scripted | replay
kind = "anthropic"
model = "claude-sonnet-4-5"
api_key_env = "ANTHROPIC_API_KEY"
max_tokens = 4096
# record = true            # wrap the provider and write a cassette for replay tests
# cassette_path = "cassettes/run.json"

# --- OpenAI ---------------------------------------------------------------
# kind = "openai_compatible"; model = "gpt-4.1"; api_key_env = "OPENAI_API_KEY"
# --- Azure OpenAI ---------------------------------------------------------
# kind = "openai_compatible"; base_url = "https://<res>.openai.azure.com/openai/deployments/<dep>"
# api_key_env = "AZURE_OPENAI_API_KEY"; auth_header = "api-key"
# extra_headers = {}  # add ?api-version via base_url if your gateway needs it
# --- Ollama / vLLM (local, no key) ----------------------------------------
# kind = "openai_compatible"; base_url = "http://localhost:11434/v1"; model = "qwen2.5-coder:14b"
# api_key_env = ""; require_api_key = false

[approval]
mode = "local"              # local | webhook (decisions only via signed POST /v1/jobs/<id>/decision)

[webhooks]                  # optional: signed events out, signed commands in (see docs/webhooks.md)
enabled = false
outbound_urls = []          # e.g. ["https://koru.example.com/hooks/gca"]
outbound_secret_env = "GCA_WEBHOOK_SECRET"
inbound_secret_env = "GCA_INBOUND_SECRET"
inbound_host = "127.0.0.1"  # anything else needs a TLS-terminating proxy in front and behind_tls_proxy = true
inbound_port = 8787
# behind_tls_proxy = true
# allow_publish = true      # false: the platform cannot ask gca to push/open PRs; use `gca publish`
# events = ["approval.requested", "job.finished"]   # empty = all

[publish]                   # this instance's (= this tenant's) publishing identity
remote = "origin"
draft = true
token_env = "GITHUB_TOKEN"  # a token scoped to this tenant's repositories only
# base = "main"
# api_base = "https://ghe.example.com/api/v3"

[servicenow]                # optional: `gca servicenow-sync` polls this table (approval happens in ServiceNow)
# instance_url = "https://dev12345.service-now.com"
# table = "x_gca_work_item"
# user_env = "SN_USER"
# password_env = "SN_PASSWORD"

[budget]
max_turns = 30
max_tool_calls = 120
max_input_tokens = 400000
max_output_tokens = 60000
max_cost_usd = 2.0
max_seconds = 900
max_files_changed = 20     # verifier fails the job if the diff is larger than this
max_lines_changed = 800

[policy]
# allowed_repos = ["/srv/repos/tenant-a"]   # work items may only target checkouts under these roots (empty = any)
allowed_commands = ["python", "python3", "pytest", "npm", "node", "npx", "ls", "cat", "grep"]
command_timeout_seconds = 300
max_blocked_actions = 5
canary_strings = []        # e.g. ["GCA-CANARY-7f3a"] planted in the environment during tests

[pricing]                  # USD per 1M tokens, by model-name prefix
input_per_million = { "claude-sonnet-4-5" = 3.0, "claude-haiku-4-5" = 1.0, "gpt-4.1" = 2.0 }
output_per_million = { "claude-sonnet-4-5" = 15.0, "claude-haiku-4-5" = 5.0, "gpt-4.1" = 8.0 }
"""


def _queue(cfg: Config) -> JobQueue:
    return JobQueue(cfg.db_path)


def _print(obj) -> None:
    if hasattr(obj, "model_dump"):
        obj = obj.model_dump()
    print(json.dumps(obj, indent=2, default=str))


def cmd_init(args, cfg):
    p = Path(args.path)
    if p.exists() and not args.force:
        print(f"{p} exists (use --force to overwrite)")
        return 1
    p.write_text(EXAMPLE_CONFIG, encoding="utf-8")
    print(f"wrote {p}")
    return 0


def cmd_submit(args, cfg):
    desc = Path(args.description_file).read_text(encoding="utf-8") if args.description_file else (args.description or "")
    try:
        repo_path = check_repo_allowed(cfg.policy.allowed_repos, args.repo)
    except PolicyViolation as e:
        print(str(e), file=sys.stderr)
        return 2
    item = WorkItem(
        title=args.title, description=desc, acceptance_criteria=args.criteria or [],
        repo_path=repo_path, base_ref=args.base,
        test_command=split_command(args.test_cmd) if args.test_cmd else WorkItem.model_fields["test_command"].default_factory(),
        allowed_paths=args.allowed_path or [], requester=args.requester, idempotency_key=args.idempotency_key,
    )
    budget = cfg.budget.model_copy()
    if args.max_cost is not None:
        budget.max_cost_usd = args.max_cost
    if args.max_turns is not None:
        budget.max_turns = args.max_turns
    if args.max_files is not None:
        budget.max_files_changed = args.max_files
    if args.max_lines is not None:
        budget.max_lines_changed = args.max_lines
    q = _queue(cfg)
    job = q.submit(item, budget)
    AuditLog(cfg.audit_dir, job.id).record(args.requester, "submitted", work_item=item.model_dump(), budget=budget.model_dump())
    print(job.id)
    return 0


def cmd_approve(args, cfg):
    if cfg.approval.mode == "webhook" and not args.override:
        print("approval.mode is 'webhook': decisions come from the approving platform. "
              "Use --override (recorded in the audit log) only for break-glass situations.", file=sys.stderr)
        return 4
    q = _queue(cfg)
    restrictions = None
    budget = {k: v for k, v in (("max_cost_usd", args.max_cost), ("max_turns", args.max_turns),
                                ("max_files_changed", args.max_files), ("max_lines_changed", args.max_lines))
              if v is not None}
    if args.allow_path or args.allow_command or budget:
        restrictions = Restrictions(allowed_paths=args.allow_path or None, allowed_commands=args.allow_command or None,
                                    budget=budget or None)
    try:
        job = q.reject(args.job_id, args.approver, args.note) if args.reject else \
            q.approve(args.job_id, args.approver, args.note, restrictions=restrictions,
                      operator_commands=cfg.policy.allowed_commands)
    except ValueError as e:
        print(f"restrictions rejected: {e}", file=sys.stderr)
        return 2
    AuditLog(cfg.audit_dir, job.id).record(args.approver, "rejected" if args.reject else "approved", note=args.note,
                                           override=bool(getattr(args, "override", False)),
                                           restrictions=restrictions.model_dump(exclude_none=True) if restrictions else None)
    print(f"{job.id} -> {job.status.value}")
    return 0


def cmd_cancel(args, cfg):
    q = _queue(cfg)
    job = q.request_cancel(args.job_id)
    AuditLog(cfg.audit_dir, job.id).record(args.by, "cancel_requested")
    print(f"{job.id} -> {job.status.value} (cancel_requested={job.cancel_requested})")
    return 0


def cmd_run(args, cfg):
    q = _queue(cfg)
    w = Worker(cfg, q)
    for j in w.recover_stale():
        print(f"recovered stale job {j.id} -> failed (worker crashed); orphaned workspace removed", file=sys.stderr)
    if args.once:
        job = w.run_once()
        if job is None:
            print("no approved jobs")
            return 0
        print(f"{job.id} -> {job.status.value}" + (f" ({job.error})" if job.error else ""))
        return 0 if job.status == JobStatus.SUCCEEDED else 2
    n = w.run_forever(poll_seconds=args.poll, max_jobs=args.max_jobs)
    print(f"processed {n} job(s)")
    return 0


def cmd_status(args, cfg):
    q = _queue(cfg)
    if args.job_id:
        job = q.get(args.job_id)
        d = job.model_dump()
        if d.get("result"):
            d["result"].pop("diff", None)
            d["result"].pop("report_markdown", None)
        _print(d)
    else:
        for j in q.list():
            print(f"{j.id}  {j.status.value:<10} {j.work_item.title[:60]}" + (f"  [{j.error}]" if j.error else ""))
    return 0


def cmd_report(args, cfg):
    job = _queue(cfg).get(args.job_id)
    if not job.result:
        print("no result yet")
        return 1
    md = job.result.report_markdown or "(report not rendered)"
    if args.diff:
        md += "\n\n## Diff\n```diff\n" + job.result.diff + "\n```\n"
    if args.out:
        Path(args.out).write_text(md, encoding="utf-8")
        print(f"wrote {args.out}")
    else:
        print(md)
    return 0


def cmd_audit(args, cfg):
    path = cfg.audit_dir / f"{args.job_id}.jsonl"
    if not path.exists():
        print("no audit log")
        return 1
    if args.verify:
        ok, msg = verify_chain(path)
        print(("OK " if ok else "TAMPERED ") + msg)
        return 0 if ok else 3
    for ev in AuditLog(cfg.audit_dir, args.job_id).events():
        print(f"{ev.seq:>4} {ev.ts} {ev.actor:<14} {ev.event:<16} {json.dumps(ev.data, default=str)[:160]}")
    return 0


def cmd_publish(args, cfg):
    from .publish import publish_job
    pub = cfg.publish
    res = publish_job(cfg, _queue(cfg), args.job_id, remote=args.remote or pub.remote, base=args.base or pub.base,
                      draft=False if args.ready else pub.draft, create_pr=not args.no_pr,
                      token_env=pub.token_env, api_base=pub.api_base, allow_failed=args.allow_failed)
    print(f"pushed {res.branch} -> {res.pushed_to}")
    if res.pr_url:
        print(f"pull request: {res.pr_url}")
    if res.note:
        print(res.note)
    return 0


def cmd_webhook_serve(args, cfg):
    import threading as _threading

    from . import webhooks as _wh
    from .inbound import make_server
    from .worker import Worker
    q = _queue(cfg)
    if not os.environ.get(cfg.webhooks.inbound_secret_env):
        print(f"refusing to start: set {cfg.webhooks.inbound_secret_env} (shared secret for inbound signatures)",
              file=sys.stderr)
        return 2
    srv = make_server(cfg, q, args.host, args.port)
    stop = _threading.Event()

    def flusher():
        while not stop.wait(args.flush_every):
            try:
                _wh.flush(cfg)
            except Exception as e:  # noqa: BLE001
                print(f"webhook flush error: {e}", file=sys.stderr)

    def worker_loop():
        w = Worker(cfg, _queue(cfg))
        while not stop.is_set():
            if w.run_once() is None:
                stop.wait(args.poll)

    threads = [_threading.Thread(target=flusher, daemon=True)]
    if args.with_worker:
        threads.append(_threading.Thread(target=worker_loop, daemon=True))
    for t in threads:
        t.start()
    host, port = srv.server_address[:2]
    print(f"gca inbound webhooks listening on http://{host}:{port}  (worker: {'on' if args.with_worker else 'off'})")
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        stop.set()
        srv.server_close()
    return 0


def cmd_webhook_deliver(args, cfg):
    from . import webhooks as _wh
    if args.requeue_dead:
        n = _wh.Outbox(cfg.db_path).requeue_dead()
        print(f"requeued {n} dead deliveries")
    print(json.dumps(_wh.flush(cfg)))
    return 0


def cmd_webhook_status(args, cfg):
    from . import webhooks as _wh
    rows = _wh.Outbox(cfg.db_path).rows(args.status)
    for r in rows[-args.limit:]:
        print(f"{r['created_at'][:19]}  {r['status']:<9} {r['type']:<22} {r['job_id'] or '-':<18} "
              f"attempts={r['attempts']} {r['url']}" + (f"  [{r['last_error']}]" if r['last_error'] else ""))
    counts: dict[str, int] = {}
    for r in rows:
        counts[r["status"]] = counts.get(r["status"], 0) + 1
    print(json.dumps(counts))
    return 0


def cmd_kill(args, cfg):
    from .inbound import set_kill_switch
    q = _queue(cfg)
    if args.state == "status":
        ks = q.kill_switch()
        print(json.dumps({"enabled": bool(ks), **(ks or {})}))
        return 0
    if args.state == "off" and not args.reason:
        print("a --reason is required to disengage the kill switch", file=sys.stderr)
        return 2
    set_kill_switch(cfg, q, args.state == "on", reason=args.reason or "", actor=args.by, via="cli")
    print(f"kill switch {args.state}")
    return 0


def cmd_servicenow_sync(args, cfg):
    import time as _time

    from .sources.servicenow import ServiceNowSource, sync_once
    source = ServiceNowSource(cfg.servicenow)
    q = _queue(cfg)
    while True:
        rep = sync_once(cfg, q, source)
        for k in ("queued", "updated", "errors"):
            for line in rep[k]:
                print(f"{k}: {line}")
        if args.once:
            return 1 if rep["errors"] else 0
        _time.sleep(args.poll or cfg.servicenow.poll_seconds)


def cmd_eval(args, cfg):
    from .evals import run_evals
    summary = run_evals(cfg, Path(args.tasks_dir), Path(args.out), only=args.only, repeats=args.repeats)
    print(summary)
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="gca", description="Governed coding agent")
    p.add_argument("--config", "-c", help="path to gca.toml (default: ./gca.toml or $GCA_CONFIG)")
    p.add_argument("--version", action="version", version=f"gca {__version__}")
    sub = p.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("init", help="write an example gca.toml"); s.add_argument("path", nargs="?", default="gca.toml"); s.add_argument("--force", action="store_true"); s.set_defaults(fn=cmd_init)

    s = sub.add_parser("submit", help="submit a work item")
    s.add_argument("--title", required=True); s.add_argument("--description"); s.add_argument("--description-file")
    s.add_argument("--criteria", action="append", help="acceptance criterion (repeatable)")
    s.add_argument("--repo", required=True); s.add_argument("--base", default="HEAD")
    s.add_argument("--test-cmd", help="e.g. 'python -m pytest -q'")
    s.add_argument("--allowed-path", action="append"); s.add_argument("--requester", default="cli")
    s.add_argument("--idempotency-key"); s.add_argument("--max-cost", type=float); s.add_argument("--max-turns", type=int)
    s.add_argument("--max-files", type=int, help="diff may touch at most N files"); s.add_argument("--max-lines", type=int, help="diff may change at most N lines")
    s.set_defaults(fn=cmd_submit)

    s = sub.add_parser("approve", help="approve (or --reject) a job"); s.add_argument("job_id"); s.add_argument("--approver", required=True); s.add_argument("--note", default=""); s.add_argument("--reject", action="store_true"); s.add_argument("--override", action="store_true", help="break-glass: approve locally even when approval.mode=webhook"); s.add_argument("--allow-path", action="append", help="enforced condition: restrict writes to this path (repeatable; must lie inside the work item's allowed paths)"); s.add_argument("--allow-command", action="append", help="enforced condition: restrict the agent to this command (repeatable; subset of the operator allow-list)"); s.add_argument("--max-cost", type=float, help="enforced condition: lower the cost budget (USD)"); s.add_argument("--max-turns", type=int); s.add_argument("--max-files", type=int); s.add_argument("--max-lines", type=int); s.set_defaults(fn=cmd_approve)
    s = sub.add_parser("cancel"); s.add_argument("job_id"); s.add_argument("--by", default="cli"); s.set_defaults(fn=cmd_cancel)
    s = sub.add_parser("run", help="run the worker"); s.add_argument("--once", action="store_true"); s.add_argument("--poll", type=float, default=2.0); s.add_argument("--max-jobs", type=int); s.set_defaults(fn=cmd_run)
    s = sub.add_parser("status"); s.add_argument("job_id", nargs="?"); s.set_defaults(fn=cmd_status)
    s = sub.add_parser("report"); s.add_argument("job_id"); s.add_argument("--out"); s.add_argument("--diff", action="store_true"); s.set_defaults(fn=cmd_report)
    s = sub.add_parser("audit"); s.add_argument("job_id"); s.add_argument("--verify", action="store_true"); s.set_defaults(fn=cmd_audit)
    s = sub.add_parser("publish", help="push a succeeded job's branch and open a draft pull request"); s.add_argument("job_id"); s.add_argument("--remote", help="default: [publish] remote"); s.add_argument("--base", help="base branch (default: [publish] base, else the remote's HEAD)"); s.add_argument("--ready", action="store_true", help="open as ready for review instead of draft"); s.add_argument("--no-pr", action="store_true", help="push only"); s.add_argument("--allow-failed", action="store_true", help="publish a failed job's branch for inspection"); s.set_defaults(fn=cmd_publish)
    s = sub.add_parser("webhook-serve", help="serve signed inbound commands (submit/decision/cancel/kill-switch)"); s.add_argument("--host"); s.add_argument("--port", type=int); s.add_argument("--with-worker", action="store_true", help="also run a worker in this process"); s.add_argument("--poll", type=float, default=2.0); s.add_argument("--flush-every", type=float, default=5.0); s.set_defaults(fn=cmd_webhook_serve)
    s = sub.add_parser("webhook-deliver", help="deliver pending outbound webhook events now"); s.add_argument("--requeue-dead", action="store_true"); s.set_defaults(fn=cmd_webhook_deliver)
    s = sub.add_parser("webhook-status", help="list outbound webhook deliveries"); s.add_argument("--status", choices=["pending", "inflight", "delivered", "dead"]); s.add_argument("--limit", type=int, default=50); s.set_defaults(fn=cmd_webhook_status)
    s = sub.add_parser("kill", help="global kill switch: stop running agents and claim nothing new"); s.add_argument("state", choices=["on", "off", "status"]); s.add_argument("--reason", default=""); s.add_argument("--by", default="cli"); s.set_defaults(fn=cmd_kill)
    s = sub.add_parser("servicenow-sync", help="poll ServiceNow for approved work items and write status back"); s.add_argument("--once", action="store_true"); s.add_argument("--poll", type=float); s.set_defaults(fn=cmd_servicenow_sync)
    s = sub.add_parser("eval", help="run golden-task evals against the configured provider"); s.add_argument("--tasks-dir", default="evals/tasks"); s.add_argument("--out", default="evals/results"); s.add_argument("--only"); s.add_argument("--repeats", type=int, default=1); s.set_defaults(fn=cmd_eval)
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    cfg = load_config(args.config)
    from . import webhooks as _wh
    _wh.install(cfg)
    return int(args.fn(args, cfg) or 0)


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
