"""Golden-task evals: does the agent actually solve bounded tasks, at what cost, how reliably?

A task is a directory:
    evals/tasks/<name>/task.json   {"title", "description", "acceptance_criteria", "test_command", "budget"?}
    evals/tasks/<name>/repo/       a small project whose tests currently FAIL

The runner copies the repo to a temp git repo, submits + approves + runs it through the real
pipeline (so the verifier, policy and audit are all exercised), and records pass/fail, turns,
tokens, cost and time. Run with `gca eval` (requires a configured provider; costs money).
"""

from __future__ import annotations

import json
import shutil
import subprocess
import tempfile
import time
from pathlib import Path

from .config import Config
from .models import Budget, JobStatus, WorkItem
from .queue import JobQueue
from .worker import Worker


def _init_repo(src: Path, dst: Path) -> None:
    shutil.copytree(src, dst)
    env = {"GIT_AUTHOR_NAME": "eval", "GIT_AUTHOR_EMAIL": "eval@localhost",
           "GIT_COMMITTER_NAME": "eval", "GIT_COMMITTER_EMAIL": "eval@localhost", "PATH": __import__("os").environ["PATH"]}
    for args in (["git", "init", "-q"], ["git", "add", "-A"], ["git", "commit", "-q", "-m", "eval fixture"]):
        subprocess.run(args, cwd=str(dst), check=True, capture_output=True, env=env)


def load_task(task_dir: Path) -> dict:
    spec = json.loads((task_dir / "task.json").read_text(encoding="utf-8"))
    spec["name"] = task_dir.name
    spec["repo_src"] = task_dir / "repo"
    return spec


def run_task(cfg: Config, spec: dict, data_dir: Path) -> dict:
    tmp = Path(tempfile.mkdtemp(prefix=f"gca-eval-{spec['name']}-"))
    repo = tmp / "repo"
    _init_repo(spec["repo_src"], repo)
    cfg = cfg.model_copy(update={"data_dir": str(data_dir)})
    q = JobQueue(cfg.db_path)
    item = WorkItem(title=spec["title"], description=spec["description"],
                    acceptance_criteria=spec["acceptance_criteria"], repo_path=str(repo),
                    test_command=spec.get("test_command", ["python", "-m", "pytest", "-q"]), requester="eval",
                    source="eval", allowed_paths=spec.get("allowed_paths", []))
    budget = Budget.model_validate(spec["budget"]) if spec.get("budget") else cfg.budget
    job = q.submit(item, budget)
    q.approve(job.id, "eval")
    t0 = time.monotonic()
    done = Worker(cfg, q).run_once()
    assert done is not None
    job = done
    secs = time.monotonic() - t0
    a, v = job.result.agent if job.result else None, job.result.verification if job.result else None
    row = {
        "task": spec["name"], "job_id": job.id, "status": job.status.value, "passed": job.status == JobStatus.SUCCEEDED,
        "stop_reason": a.stop_reason.value if a else None, "claimed_success": a.claimed_success if a else None,
        "turns": a.usage.turns if a else 0, "tool_calls": a.usage.tool_calls if a else 0,
        "input_tokens": a.usage.input_tokens if a else 0, "output_tokens": a.usage.output_tokens if a else 0,
        "cost_usd": round(a.usage.cost_usd, 4) if a else 0.0, "seconds": round(secs, 1),
        "files_changed": len(v.files_changed) if v else 0, "insertions": v.insertions if v else 0,
        "deletions": v.deletions if v else 0, "flags": v.flags if v else [], "blocked_actions": a.blocked_actions if a else 0,
        "error": job.error, "provider": job.result.provider if job.result else "", "model": job.result.model if job.result else "",
    }
    q.close()
    shutil.rmtree(tmp, ignore_errors=True)
    return row


def run_evals(cfg: Config, tasks_dir: Path, out_dir: Path, *, only: str | None = None, repeats: int = 1) -> str:
    out_dir.mkdir(parents=True, exist_ok=True)
    stamp = time.strftime("%Y%m%d-%H%M%S")
    data_dir = out_dir / f"{stamp}-data"
    rows: list[dict] = []
    for task_dir in sorted(p for p in tasks_dir.iterdir() if (p / "task.json").exists()):
        if only and only not in task_dir.name:
            continue
        spec = load_task(task_dir)
        for r in range(repeats):
            row = run_task(cfg, spec, data_dir)
            row["repeat"] = r
            rows.append(row)
            print(f"[{row['task']}#{r}] {'PASS' if row['passed'] else 'FAIL'} turns={row['turns']} "
                  f"cost=${row['cost_usd']} {row['seconds']}s {row['error'] or ''}")
    (out_dir / f"{stamp}.json").write_text(json.dumps(rows, indent=1), encoding="utf-8")
    md = summarize(rows)
    (out_dir / f"{stamp}.md").write_text(md, encoding="utf-8")
    return md


def summarize(rows: list[dict]) -> str:
    if not rows:
        return "no tasks run"
    n = len(rows)
    passed = sum(1 for r in rows if r["passed"])
    lines = [f"# Eval results — {passed}/{n} passed ({100 * passed / n:.0f}%)", "",
             "| task | run | result | stop | turns | tools | tokens in/out | cost | secs | Δlines | flags |",
             "|---|---|---|---|---|---|---|---|---|---|---|"]
    for r in rows:
        lines.append(f"| {r['task']} | {r.get('repeat', 0)} | {'PASS' if r['passed'] else 'FAIL'} | {r['stop_reason']} | "
                     f"{r['turns']} | {r['tool_calls']} | {r['input_tokens']}/{r['output_tokens']} | ${r['cost_usd']} | "
                     f"{r['seconds']} | +{r['insertions']}/−{r['deletions']} | {', '.join(r['flags']) or '-'} |")
    cost = sum(r["cost_usd"] for r in rows)
    lines += ["", f"Total cost ${cost:.4f}; mean turns {sum(r['turns'] for r in rows) / n:.1f}; "
                  f"provider {rows[0]['provider']}/{rows[0]['model']}"]
    return "\n".join(lines)
