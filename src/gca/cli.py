"""`gca` command line: submit -> approve -> run -> report, plus audit and eval."""

from __future__ import annotations

import argparse
import json
import shlex
import sys
from pathlib import Path

from . import __version__
from .audit import AuditLog, verify_chain
from .config import Config, load_config
from .models import JobStatus, WorkItem
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
    item = WorkItem(
        title=args.title, description=desc, acceptance_criteria=args.criteria or [],
        repo_path=str(Path(args.repo).resolve()), base_ref=args.base,
        test_command=shlex.split(args.test_cmd) if args.test_cmd else WorkItem.model_fields["test_command"].default_factory(),
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
    q = _queue(cfg)
    job = q.reject(args.job_id, args.approver, args.note) if args.reject else q.approve(args.job_id, args.approver, args.note)
    AuditLog(cfg.audit_dir, job.id).record(args.approver, "rejected" if args.reject else "approved", note=args.note)
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
    recovered = q.recover_stale(cfg.stale_heartbeat_seconds)
    for j in recovered:
        print(f"recovered stale job {j.id} -> failed", file=sys.stderr)
    w = Worker(cfg, q)
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
    res = publish_job(cfg, _queue(cfg), args.job_id, remote=args.remote, base=args.base, draft=not args.ready,
                      create_pr=not args.no_pr, allow_failed=args.allow_failed)
    print(f"pushed {res.branch} -> {res.pushed_to}")
    if res.pr_url:
        print(f"pull request: {res.pr_url}")
    if res.note:
        print(res.note)
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

    s = sub.add_parser("approve", help="approve (or --reject) a job"); s.add_argument("job_id"); s.add_argument("--approver", required=True); s.add_argument("--note", default=""); s.add_argument("--reject", action="store_true"); s.set_defaults(fn=cmd_approve)
    s = sub.add_parser("cancel"); s.add_argument("job_id"); s.add_argument("--by", default="cli"); s.set_defaults(fn=cmd_cancel)
    s = sub.add_parser("run", help="run the worker"); s.add_argument("--once", action="store_true"); s.add_argument("--poll", type=float, default=2.0); s.add_argument("--max-jobs", type=int); s.set_defaults(fn=cmd_run)
    s = sub.add_parser("status"); s.add_argument("job_id", nargs="?"); s.set_defaults(fn=cmd_status)
    s = sub.add_parser("report"); s.add_argument("job_id"); s.add_argument("--out"); s.add_argument("--diff", action="store_true"); s.set_defaults(fn=cmd_report)
    s = sub.add_parser("audit"); s.add_argument("job_id"); s.add_argument("--verify", action="store_true"); s.set_defaults(fn=cmd_audit)
    s = sub.add_parser("publish", help="push a succeeded job's branch and open a draft pull request"); s.add_argument("job_id"); s.add_argument("--remote", default="origin"); s.add_argument("--base", help="base branch (default: the remote's HEAD)"); s.add_argument("--ready", action="store_true", help="open as ready for review instead of draft"); s.add_argument("--no-pr", action="store_true", help="push only"); s.add_argument("--allow-failed", action="store_true", help="publish a failed job's branch for inspection"); s.set_defaults(fn=cmd_publish)
    s = sub.add_parser("servicenow-sync", help="poll ServiceNow for approved work items and write status back"); s.add_argument("--once", action="store_true"); s.add_argument("--poll", type=float); s.set_defaults(fn=cmd_servicenow_sync)
    s = sub.add_parser("eval", help="run golden-task evals against the configured provider"); s.add_argument("--tasks-dir", default="evals/tasks"); s.add_argument("--out", default="evals/results"); s.add_argument("--only"); s.add_argument("--repeats", type=int, default=1); s.set_defaults(fn=cmd_eval)
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    cfg = load_config(args.config)
    return int(args.fn(args, cfg) or 0)


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
