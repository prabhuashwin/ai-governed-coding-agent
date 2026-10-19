"""Worker: claims approved jobs and runs the full pipeline for each one.

claim -> workspace -> agent loop -> commit -> independent verification -> report -> finish.
Any exception becomes a FAILED job with the error recorded; the workspace is always cleaned up
(the branch is kept so a human can inspect it).
"""

from __future__ import annotations

import time
import traceback
from collections.abc import Callable

from . import webhooks
from .agent import AgentLoop
from .audit import AuditLog
from .config import Config
from .models import Job, JobStatus, RunResult, StopReason
from .policy import BudgetTracker, PolicyEngine
from .providers import build_provider, estimate_cost
from .providers.base import Provider
from .queue import JobQueue
from .report import render_report
from .tools import ToolExecutor
from .verifier import Verifier
from .workspace import cleanup_orphan_worktree, make_runner, make_workspace


class Worker:
    def __init__(self, config: Config, queue: JobQueue, *, provider_factory: Callable[[], Provider] | None = None,
                 worker_id: str | None = None):
        self.cfg = config
        self.queue = queue
        self.provider_factory = provider_factory or (lambda: build_provider(config.provider))
        self.worker_id = worker_id or config.worker_id
        webhooks.install(config)

    def recover_stale(self) -> list[Job]:
        """Fail jobs whose worker died (stale heartbeat), remove their orphaned workspaces, and record a
        `finished` event so audit consumers and webhooks learn the outcome."""
        recovered = self.queue.recover_stale(self.cfg.stale_heartbeat_seconds)
        for job in recovered:
            audit = AuditLog(self.cfg.audit_dir, job.id, redactions=self.cfg.policy.canary_strings)
            ws_path = next((e.data.get("path") for e in reversed(audit.events())
                            if e.event == "workspace_created" and e.data.get("path")), None)
            cleaned = False
            if ws_path:
                cleaned = cleanup_orphan_worktree(job.work_item.repo_path, ws_path)
            audit.record(f"worker:{self.worker_id}", "recovered", reason=job.error, orphan_workspace=ws_path,
                         orphan_removed=cleaned)
            audit.record(f"worker:{self.worker_id}", "finished", status=job.status.value, error=job.error)
        return recovered

    def run_once(self) -> Job | None:
        job = self.queue.claim_next(self.worker_id)
        if job is None:
            return None
        try:
            return self.process(job)
        finally:
            if self.cfg.webhooks.enabled:
                try:
                    webhooks.flush(self.cfg)
                except Exception:  # noqa: BLE001 - delivery failures stay in the outbox
                    pass

    def run_forever(self, poll_seconds: float = 2.0, max_jobs: int | None = None) -> int:
        done = 0
        while max_jobs is None or done < max_jobs:
            if self.run_once() is None:
                time.sleep(poll_seconds)
            else:
                done += 1
        return done

    # ---------------------------------------------------------------- core
    def process(self, job: Job) -> Job:
        audit = AuditLog(self.cfg.audit_dir, job.id, redactions=self.cfg.policy.canary_strings)
        audit.record(f"worker:{self.worker_id}", "claimed", attempt=job.attempts, work_item=job.work_item.model_dump())
        wi = job.work_item
        ws = None
        result = RunResult(job_id=job.id)
        try:
            ws = make_workspace(self.cfg.workspace, wi.repo_path, job.id, wi.base_ref)
            result.branch = ws.branch
            audit.record(f"worker:{self.worker_id}", "workspace_created", branch=ws.branch, base=ws.base_commit,
                         path=str(ws.root),
                         kind=self.cfg.workspace.kind, image=self.cfg.workspace.image if self.cfg.workspace.kind == "container" else None)

            policy = PolicyEngine(self.cfg.policy, ws.root, wi.allowed_paths)
            tracker = BudgetTracker(job.budget)
            executor = ToolExecutor(ws, policy, wi.test_command)
            provider = self.provider_factory()
            result.provider, result.model = provider.name, provider.model

            loop = AgentLoop(
                provider, executor, tracker, audit,
                cost_fn=lambda i, o: estimate_cost(self.cfg, provider.model, i, o),
                max_blocked_actions=self.cfg.policy.max_blocked_actions,
                cancel_check=lambda: self.queue.cancel_requested(job.id),
                kill_check=lambda: self.queue.kill_switch() is not None,
                heartbeat=lambda: self.queue.heartbeat(job.id),
            )
            outcome = loop.run(wi)
            result.agent = outcome

            commit = ws.commit_all(f"gca: {wi.title}\n\nJob {job.id}. Agent summary:\n{outcome.summary[:2000]}")
            result.diff = ws.diff()
            audit.record(f"worker:{self.worker_id}", "committed", commit=commit, changed_files=ws.changed_files())

            if outcome.stop_reason in (StopReason.CANCELLED, StopReason.KILL_SWITCH):
                why = "kill_switch" if outcome.stop_reason == StopReason.KILL_SWITCH else "cancelled by request"
                job = self.queue.finish(job.id, JobStatus.CANCELLED, result=result, error=why)
                audit.record(f"worker:{self.worker_id}", "finished", status=job.status.value, error=why)
                return job

            job = self.queue.transition(job.id, JobStatus.VERIFYING)
            self.queue.heartbeat(job.id)
            verifier = Verifier(wi.repo_path, ws.base_commit, ws.branch, wi.test_command, self.cfg.policy,
                                run_fn=make_runner(self.cfg.workspace, job.id),
                                max_files_changed=job.budget.max_files_changed,
                                max_lines_changed=job.budget.max_lines_changed,
                                allowed_paths=wi.allowed_paths)
            verification = verifier.verify()
            result.verification = verification
            audit.record("verifier", "verified", passed=verification.passed, flags=verification.flags,
                         exit_code=verification.exit_code, files_changed=verification.files_changed)

            policy_clean = outcome.blocked_actions == 0 or not self.cfg.policy.fail_on_any_blocked_action
            ok = verification.passed and outcome.stop_reason == StopReason.FINISHED and policy_clean
            final = JobStatus.SUCCEEDED if ok else JobStatus.FAILED
            job.status = final  # for the report
            result.report_markdown = render_report(job, result)
            err = None if ok else _failure_reason(outcome.stop_reason, verification.passed, verification.flags,
                                                  outcome.blocked_actions if not policy_clean else 0)
            job = self.queue.finish(job.id, final, result=result, error=err)
            audit.record(f"worker:{self.worker_id}", "finished", status=job.status.value, error=err)
            return job
        except Exception as e:  # noqa: BLE001 - anything else is a FAILED job, never a crashed worker
            tb = traceback.format_exc()[-3000:]
            audit.record(f"worker:{self.worker_id}", "internal_error", error=str(e), traceback=tb)
            try:
                cur = self.queue.get(job.id)
                if cur.status in (JobStatus.RUNNING, JobStatus.VERIFYING):
                    if result.agent is None:
                        result.agent = None
                    job = self.queue.finish(job.id, JobStatus.FAILED, result=result, error=f"internal_error: {e}")
                    audit.record(f"worker:{self.worker_id}", "finished", status=job.status.value,
                                 error=f"internal_error: {e}")
            except Exception:  # pragma: no cover
                pass
            return job
        finally:
            if ws is not None:
                ws.cleanup(keep_branch=True)


def _failure_reason(stop: StopReason, verified: bool, flags: list[str], blocked: int = 0) -> str:
    if stop != StopReason.FINISHED:
        return f"agent_stopped:{stop.value}"
    if blocked:
        return f"blocked_actions:{blocked}"
    if flags:
        return "verification_failed:" + ",".join(flags)
    if not verified:
        return "verification_failed:tests"
    return "failed"
