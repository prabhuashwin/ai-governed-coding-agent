# Changelog

Versions are git tags `v<version>`. Review rounds are summarised in `docs/how-to-test.md` ("External review").

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
