# Changelog

Versions are git tags `v<version>`. Review rounds are summarised in `docs/how-to-test.md` ("External review").

## 0.3.0 — unreleased

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
