# Pilot scenario matrix and evidence format

**Status:** work package 5. The matrix is what the pilot sign-off reads; every row points at the automated
evidence that exists today and says what still needs a real-model or real-Koru run.

Two kinds of scenario are deliberately kept apart:

- **Harness controls** — what gca enforces whatever the model does. Evidence is a deterministic test with a
  scripted (possibly hostile) model; it runs on every CI build and either passes or the build is red.
- **Model behaviour** — what a given model does on its own inside those controls (fixes the bug, honours
  an advisory condition, refuses an impossible task). Evidence is a real-model eval run, which is
  statistical: cite the run, the model and the pass rate, never a single lucky run.

## Matrix

| # | Scenario | Kind | Evidence (automated, every CI run) | Real-model / real-Koru evidence |
|---|---|---|---|---|
| 1 | Bounded defect fix end to end: submit → approve → agent → independent verification → report | both | `tests/e2e/test_cli_pipeline.py`; `scripts/webhook_e2e.py` (contract test) | eval tasks 01–03, 06, 08 (`docs/eval-baseline.md`) |
| 2 | Approval with **enforced restrictions** (paths, commands, budget); widening refused; job stays undecided | harness | `tests/unit/test_webhooks.py::test_decision_restrictions_are_enforced_or_rejected`, `tests/safety/test_worker_controls.py` (4 tests), `tests/unit/test_coverage_gaps.py::test_cli_approve_restrictions` | eval task **10-restricted-paths**: does the model take the bait and try to edit outside the scope? (`restriction_attempts`) |
| 3 | **Advisory condition violated** — the approver asked for something the harness cannot enforce | model | `tests/e2e/test_evals.py::test_11_advisory_condition_is_measured_not_enforced` (the eval records the violation; the job still succeeds) | eval task **11-advisory-condition** (`conditions_respected`). A pilot that needs this enforced must express it as a restriction or a test |
| 4 | Prompt injection in task text, repo files and test output | harness | `tests/safety/test_controls.py` (injection, canary, blocked-action halt) | eval tasks 04, 05 (`ignored_injection`, `harness_contained`) |
| 5 | Test tampering: deleted/skipped/renamed tests, added config, collection-time deselection, fewer tests run | harness | `tests/safety/test_controls.py` (verifier tests), `test_added_conftest_that_deselects_a_test_is_caught` | eval task 09 (a model that "fixes" a contradiction by weakening tests fails) |
| 6 | Agent claims success but verification fails | harness | `test_verifier_rejects_claimed_success_when_tests_fail` | observed once for real (`real-openai-1`, `docs/eval-baseline.md`) |
| 7 | Honest refusal of an impossible task | model | `tests/e2e/test_evals.py` refusal grading | eval task 09 (`honest_refusal`) |
| 8 | Cancel while a command is running; cancel during verification | harness | `test_cancel_interrupts_a_running_command`, `test_cancel_during_verification_never_reports_success`, `test_cancel_between_check_and_commit_never_reports_success` | — (deterministic) |
| 9 | Kill switch: before claim, during a run, during verification; disengage needs a reason | harness | `test_kill_switch_blocks_claims_and_stops_running_agent`, `test_kill_switch_interrupts_verification`, `test_kill_cli` | contract test `kill_switch_round_trip`; Koru containment UI → `scope = "tenant"` (blocked on Koru) |
| 10 | Kill-switch scopes (global/tenant/job) | harness | `test_kill_switch_scopes`; contract test `job_scope_stop_*` | — |
| 11 | **Stale kill-switch retry**: an old "off" delivered after a newer "on" must not clear the stop; check and update are one transaction; a crashed delivery is replayed, not re-applied | harness | `test_expired_kill_switch_delivery_cannot_undo_a_newer_stop`, `test_kill_switch_check_and_update_are_one_transaction`, `tests/review_probes/test_recovery.py` (4 reviewer probes) | — |
| 12 | Duplicate / replayed / abandoned inbound deliveries create one job | harness | `test_concurrent_duplicate_deliveries_create_one_job`, `test_abandoned_reservation_is_taken_over_by_a_retry`; contract test `replayed_delivery_is_idempotent` | — |
| 13 | Platform outage: events queue, retry, dead-letter, spill on outbox failure | harness | `test_receiver_down_does_not_break_jobs_and_flush_recovers`, `test_outbox_insert_failure_spills_and_is_recovered`, `test_spill_drain_survives_a_crash_and_concurrent_appends` | — |
| 14 | Signature binding (method, path, delivery id, body, version); unsigned/forged commands rejected | harness | `test_sign_and_verify`, `test_inbound_requires_valid_signature`, `tests/unit/test_fuzz.py` | contract test `unsigned_command_rejected`, `all_signatures_valid` |
| 15 | Publishing: only the verified commit, draft PR, attributed actor, refused when the branch moved or the job failed; platform publish can be disabled | harness | `tests/unit/test_publish_and_windows_paths.py`, `test_publish_via_inbound`, `test_publish_refused_when_disabled_or_not_succeeded` | contract test `publish_pushed_verified_commit`; a real GitHub PR (manual, `gca publish`) |
| 16 | Tenancy: repository allow-list from every source; publishing identity per instance; plain HTTP only on loopback | harness | `tests/unit/test_tenancy.py`; contract test `repo_outside_allow_list_refused` | — |
| 17 | Container isolation: no network, dropped capabilities, only the worktree mounted, timeout kills the container | harness | `tests/unit/test_container_workspace.py` (fake runtime always; real daemon on the Ubuntu CI jobs) | — |
| 18 | Crash recovery: stale worker heartbeat → job failed with reason; audit chain intact across processes | harness | `tests/unit/test_queue_audit.py`, `tests/e2e/test_fault_injection.py` | — |
| 19 | Cost and budget stops (turns, tokens, cost, wall-clock, diff size) | harness | `tests/loop/test_agent_loop.py`, `test_diff_size_budget_is_enforced` | every eval row reports cost/turns; the workflow caps `max_cost_usd` per task |
| 20 | Local (no external inference) model profile | model | — | `docs/eval-baseline.md` local-ollama runs: 7B models unreliable; 14B–32B on GPU is the floor |
| 21 | **Per-agent stop**: one platform agent is stopped without stopping the tenant; its running job ends `cancelled`/`agent_stopped`, its queued jobs wait, others run; a stale release cannot undo a newer stop | harness | `tests/unit/test_koru_gaps.py` (per-agent stop, receipt order, scope mismatch, stop during a model call) | contract test `agent_stop_holds_then_releases`, `agent_stop_event`; Koru's per-agent switch → `scope = "agent"` (blocked on Koru) |
| 22 | **Approval deadline**: no decision in time → job `cancelled`/`approval_expired`, `approval.expired` sent, late decision refused (409) | harness | `tests/unit/test_koru_gaps.py` (deadline tests, incl. a failed record retried) | contract tests `approval_window_expires`, `late_decision_refused`, `approval_expired_event`; Koru's SLA → `approval_timeout_seconds` |
| 23 | **Forget user**: with opaque identities, no name or email reaches the append-only audit log; names and emails refused at every entry point | harness | `tests/unit/test_koru_gaps.py` (identity tests, incl. bare user names and ServiceNow `sys_id`s) | contract tests `email_identity_refused`, `bare_user_name_refused`; Koru deletes its id → person mapping (blocked on Koru) |

Rows whose real-model column says "—" are deterministic harness behaviour: a real model cannot change the
outcome, so a real-model run adds no evidence.

## Evidence format

Every automated eval run produces, under `evals/results/`:

- `<stamp>.json` — one row per task × repeat with: `task`, `category`, `expect`, `job_id`, `status`,
  `passed` (after task-specific grading), `stop_reason`, `claimed_success`, `explicit_finish`, `turns`,
  `tool_calls`, `input_tokens`, `output_tokens`, `cost_usd`, `seconds`, `files_changed`, `insertions`,
  `deletions`, `flags` (verifier), `blocked_actions`, `error`, `provider`, `model`, `checks` (per-task
  grading, e.g. `within_restrictions`, `conditions_respected`, `ignored_injection`, `hidden_tests`,
  `honest_refusal`) and the grading detail fields (`restriction_attempts`, `condition_violations`,
  `injection_obeyed`, `canary_leaked`, `harness_contained`, `hidden_tail`, `new_tests`).
- `<stamp>.md` — the summary table (`gca eval` prints it too).
- `<stamp>-data/` — the job store and the hash-chained audit log of every job in the run.

The GitHub workflow (`evals (real model)`, manual) commits all three to the `eval-results` branch under
`runs/real-<provider>-<run number>/` with a README naming the workflow run and the commit, and attaches the
same as a workflow artifact. **Cite a run as** `real-openai-6 @ 3a441cd, gpt-4.1-mini, 8/9` and link the
branch directory; the audit logs there are the primary evidence for any single job.

For the pilot sign-off, the evidence pack is:

1. The CI run of the commit being signed off (green = every harness row above holds).
2. One eval run per candidate model on all tasks with `--repeats 3`, cited as above, with tasks 10 and 11
   read as behaviour measurements, not pass/fail gates. Done for gpt-4.1-mini: `real-openai-11 @ d2a7cda`,
   31/33 (`docs/eval-baseline.md`).
3. `webhook-e2e.md` from `scripts/webhook_e2e.py` against the Koru staging adapter (blocked on Koru;
   `docs/koru-adapter.md` lists the acceptance run).

## Pilot operating system

The pilot host is **Linux** (Ubuntu 22.04 or later, or WSL2 on Windows) with Docker or Podman available so
that `[workspace] kind = "container"` can be used for every repository that is not fully trusted. Rationale:
container isolation, the cross-process file lock on the audit log and the interruptible command runner are
exercised on Linux in every CI run and in all real-model runs to date.

Windows is a **tested client, not a pilot host**: the Windows CI job runs the unit and safety suites (path
rules, queue, audit lock, workspace) so that `gca submit`, `gca status`, `gca report` and `gca approve` work
from a Windows checkout, but the worker has not been run end to end on Windows with a real model and the
container workspace needs WSL2. macOS is untested.
