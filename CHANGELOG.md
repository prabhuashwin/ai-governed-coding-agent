# Changelog

Versions are git tags `v<version>`. Review rounds are summarised in `docs/how-to-test.md` ("External review").

## 0.3.0 — unreleased

Fifth external review (of `e05ad46`): eight findings, all fixed with regression tests
(`tests/unit/test_review_round5.py`; table in `docs/how-to-test.md`).

- Stops now kill the whole process tree of a running command and never hang draining its pipes; a stop that
  lands while a short command runs still counts, and a job can no longer end `succeeded` with the kill switch
  engaged (`complete()` re-checks it in the success transaction).
- `gca prune` ages inbound deliveries by their immutable first-received time, not the lease.
- Superseded kill-switch deliveries replay their 409 instead of failing.
- Non-finite budget values are rejected at every boundary (`Budget`, `Restrictions`, inbound submit/decision).
- An approval that narrows `allowed_commands` no longer blocks `run_tests`.
- Publishing: GitHub errors after the push return 502 (never a dropped connection); GitHub Enterprise remotes
  matching the configured `api_base` host get their pull request.

Work package 2 of the Koru pilot plan: the worker controls the platform needs.

- **Enforced approval restrictions.** A decision (webhook or `gca approve`) may carry `restrictions`
  (`allowed_paths`, `allowed_commands`, `budget`) that only narrow what the work item and operator policy
  allow; they are applied in the approval transaction and enforced by the policy engine, the agent loop and
  the verifier. Widening is refused (422 / exit 2) and the job stays awaiting approval. Free-text
  `conditions` remain advisory and appear in the report. `approval.decided` events carry both.
- **Prompt interruption.** Cancel and the kill switch now kill a command or verifier test run in flight
  (polled every 0.25 s, host and container workspaces) instead of waiting for it to finish; the job ends
  `cancelled` (never `succeeded`) with the verification flagged `interrupted`.
- **Inbound publish.** `POST /v1/jobs/{id}/publish` (signed, `actor` required) pushes the verified commit and
  opens a draft PR like `gca publish`, emitting `job.published`; `[webhooks] allow_publish = false` disables it.
  Push and PR audit entries now name the requesting actor.
- **Kill-switch scopes.** `scope` is `global` or `tenant` (this instance) or `job` with `job_id`, which maps
  onto cancellation. Events and audit entries carry the scope.

Work package 3: one instance per tenant (`docs/multi-tenancy.md`).

- **Repository allow-list.** `[policy] allowed_repos` restricts every submission (webhook 403, CLI exit 2,
  ServiceNow record error) to checkouts under the listed roots, symlinks resolved. Empty keeps today's
  behaviour for single-developer use.
- **Publishing identity.** New `[publish]` section (`remote`, `base`, `draft`, `token_env`, `api_base`) used
  by `gca publish` and the inbound publish command; a request cannot choose the token or API endpoint.
- **TLS placement.** The inbound server refuses to bind a non-loopback address unless
  `[webhooks] behind_tls_proxy = true`.

Work package 4: the Koru adapter (`docs/koru-adapter.md`).

- **Ownership.** The adapter is Koru-side code owned by the Koru platform team; this repository owns the
  contract, the contract test and the samples. The seam: gca guarantees the samples, the adapter is tested
  against them.
- **Contract test.** `scripts/webhook_e2e.py` now exercises the whole surface over real signed HTTP
  (submit, replay, allow-list refusal, approval with enforced restrictions, lifecycle, status, publish,
  instance- and job-scope stops) and writes `webhook-e2e.contract.json`; the committed snapshot is
  `docs/webhook-contract-samples.json`, and CI fails if event shapes drift from it.

Work package 5: validation (`docs/pilot-scenarios.md`).

- **Scenario matrix** mapping twenty pilot scenarios (including advisory conditions violated and stale
  kill-switch retries) to their automated and real-model evidence; **evidence format** for eval runs and the
  sign-off pack; **pilot OS**: Linux host (WSL2 acceptable), Windows as a tested client only.
- **Eval tasks 10 and 11** with runner support for approval `restrictions` and `conditions`:
  `within_restrictions` / `restriction_attempts` measure whether a model tries to write outside an enforced
  scope; `forbidden_diff` / `conditions_respected` measure whether it honours an advisory condition.

Work package 6: operations.

- **Pinned dependencies**: `requirements.lock` (runtime) and `requirements-dev.lock` (CI); every workflow
  installs from the lock.
- **Retention**: `[retention]` (`jobs_days`, `outbox_days`, `inbound_days`) and `gca prune [--dry-run]`.
  Audit logs are never pruned by gca.
- **Operator runbook** (`docs/runbook.md`) and **security sign-off** (`docs/security-signoff.md`) for an
  integrated deployment.
- `.coverage` is no longer tracked. Licence stays `Proprietary` pending a decision.

## 0.2.0 — 2026-10-19

The release that closes four rounds of external security and correctness review of 0.1.0 (31 findings:
9 P1, 22 P2). Every finding has a regression test; the reviewer's own reproduction probes are kept under
`tests/review_probes/`.

**Behaviour changes operators must know**

- Webhook signatures are version 2: the MAC binds version, timestamp, method, path, delivery id and body.
  Version 1 is rejected. `docs/webhooks.md` has the new verification samples; the platform side must be updated.
- The verifier runs the test command on the base commit as well as the branch, and fails a job that executes
  fewer tests than the base. Verification takes roughly twice as long.
- Publishing pushes the exact verified commit and refuses if the branch moved since, or if the job predates
  commit pinning; `--allow-failed` overrides both.
- Inbound deliveries whose first copy died are taken over after 120 s and re-run, except the kill switch, whose
  change and delivery outcome commit together; a stale kill-switch delivery is replayed or refused as superseded.
- Database schema: `controls.delivery_id` and `inbound_deliveries.first_received_at` are added on first open.

**Fixed**

- Verifier: added test-config files, collection-time deselection, writes and renames outside `allowed_paths`,
  test-environment detection in non-test code.
- Isolation: host git pinned to trusted metadata (no agent-controlled hooks); hidden eval tests sandboxed.
- Webhooks: replay across method/path/job, duplicate deliveries, lost outbox events (spill file), unredacted
  `approval.requested`, crashed reservations, stale kill-switch takeovers.
- Correctness: audit chain across processes, cancel/finish races (one transaction, report rendered after the
  decision), budget checks on the finishing turn (tokens, cost, elapsed time), eval grading of refusals,
  ServiceNow `verifying` records, linked-worktree repositories, concurrent schema migration.

## 0.1.0 — 2026-10-18

First pilot-grade build: work item → approval → isolated agent run → independent verification → report →
draft PR; signed webhooks; ServiceNow adapter; golden-task evals with a measured baseline.
