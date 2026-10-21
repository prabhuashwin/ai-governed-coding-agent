# Changelog

Versions are git tags `v<version>`. Review rounds are summarised in `docs/how-to-test.md` ("External review").

## 0.3.0 — unreleased

Koru integration gaps (`tests/unit/test_koru_gaps.py`, contract test now 26 checks):

- **Per-agent stop.** Work items may carry `agent_id` (the platform agent that submitted them). The kill switch
  gains `scope = "agent"` + `agent_id`: no further tool call of that agent's running jobs starts, a command in
  flight is killed within about a second (a model call in flight returns first and is not acted on), and the jobs
  end `cancelled` with error `agent_stopped` (never `succeeded`, also when the stop lands just before success
  commits); its queued jobs are not claimed until it is released (with a reason); other agents are unaffected;
  a stale release cannot undo a newer stop. `gca kill on|off --agent <id>`; `gca kill status` lists stopped
  agents. `agent_id` appears on `job.submitted`, `approval.requested`, `job.finished` and `control.kill_switch`.
- **Approval deadline.** `[approval] timeout_seconds` (operator ceiling) and `approval_timeout_seconds` on a
  submission (may only shorten it; `gca submit --approval-timeout`). A decision after the deadline is refused
  (409 / exit 2) inside the decision transaction; overdue jobs are cancelled with error `approval_expired` by
  the worker and by `webhook-serve`, recorded, and reported as the new `approval.expired` event. Without
  either setting jobs wait indefinitely, as before.
- **Opaque identities ("Forget user").** `[privacy] opaque_identities = true` (+ `identity_pattern`) requires
  every person — requester (then mandatory), approver, actor, by — to be an opaque platform user id on the
  webhook API, the CLI and the ServiceNow sync (which then records only `sys_id`s and refuses anything else).
  `identity_pattern` is required in this mode and may not accept an email address or a spaced name; whatever
  does not match it — emails, names, user names — is refused before anything is written and is not echoed back.
  The platform deleting its id → person mapping is then the whole of "forget user" for gca. Free text is still
  recorded as given.
- Schema: `jobs.agent_id` and `jobs.approval_expires_at` are added on first open (older rows: no agent, no
  deadline). `gca approve` now exits 2 with a message instead of a traceback when a job is already decided.
- Pre-merge independent review of the above found six issues, all fixed with tests: kill-switch requests (the
  instance's too — this predates 0.3.0) are now ordered by when they were first received, not when they were
  applied, so an older "off" applied late can no longer make a newer "on" look stale; opaque mode requires an
  explicit `identity_pattern` and refuses ServiceNow people that are not `sys_id`s; `agent_id`/`job_id` with a
  mismatched `scope` is refused instead of acting on the instance switch; an expiry whose audit record fails is
  retried by the next sweep instead of being lost; no tool call starts after a stop that lands during a model
  call; and the docs no longer overstate stop latency or how often a plain worker sweeps expiries.
- Outbox: a transient SQLite lock error while claiming or recording a delivery (seen on Windows CI with
  concurrent deliverers) is retried instead of aborting the delivery pass, which had left the posted event in
  flight to be posted again after its lease and skipped the rest of the batch.

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

Sixth external review (of `db227b2`): six findings, all fixed with regression tests
(`tests/unit/test_review_round6.py`).

- Stops now kill children that outlived the command itself (POSIX session kill regardless of the leader;
  Windows job objects), and pipe shutdown is bounded on Windows too.
- `gca prune` works on databases that have not yet received the `first_received_at` migration.
- Git remotes are parsed, not regex-matched: ports and credentials are ignored, hostnames compared exactly.
- Budget restrictions with huge integers are rejected (422) instead of raising.
- `docs/eval-baseline.md` now states plainly that two real-model jobs shipped an unauthorized write to a job
  branch (an emptied test file, failed by the verifier; an extra `NOTES.md`, which succeeded and is caught only
  by human review).

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
