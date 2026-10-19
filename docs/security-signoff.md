# Security sign-off for an integrated (Koru) deployment

**Status:** work package 6. This is the document a security reviewer signs. It states what gca claims, how
each claim is evidenced, what the reviewer must verify in the deployment itself, and what remains open.
Version under review: see `CHANGELOG.md` (unreleased 0.3.0 on top of 0.2.0, which closed four external
review rounds — 31 findings, all with regression tests).

## 1. System boundary

```
 Koru (identity, approvals, channels) ──signed HTTPS──► TLS proxy ──HTTP loopback──► gca instance (one per tenant)
                                       ◄──signed HTTPS──                               │
                                                                                       ├── git worktree of an allowed repo
                                                                                       ├── container per command (no network)
                                                                                       └── model provider / gateway (HTTPS out)
```

Inside the boundary: the gca process, its data directory, the tenant's repository checkouts, the container
runtime. Outside: Koru, GitHub (publish only), the model provider. Code and task text cross to the model
provider; that is the one data flow out of the boundary besides events to Koru.

## 2. Claims and evidence

| # | Claim | Control | Evidence |
|---|---|---|---|
| C1 | No change reaches a repository's main line without a human: every job needs an approval, every merge is a human action on a draft PR | approval gate; publish opens drafts only | `tests/unit/test_queue_audit.py::test_unapproved_jobs_are_never_claimed`; publish tests; `report.py` footer |
| C2 | The approver may only narrow (paths, commands, budget), never widen; a caller may lower but never raise budgets | `Restrictions.apply`, inbound budget clamp | `test_decision_restrictions_are_enforced_or_rejected`, `test_caller_cannot_raise_budget_above_operator_limits`, `tests/safety/test_worker_controls.py` |
| C3 | The agent cannot write outside its worktree/allowed paths, run commands outside the allow-list, use shell operators, or see secrets — and the verifier re-checks the diff independently of the tools | policy engine; scrubbed env; verifier | `tests/unit/test_policy.py`, `tests/safety/test_controls.py`, `test_command_writes_outside_allowed_paths_fail_verification` |
| C4 | Test tampering fails the job: deleted/skipped/renamed tests, modified or added test config, fewer tests executed than at the base commit, test-environment detection in non-test code | verifier baseline run + tamper flags | `tests/safety/test_controls.py` (10 verifier tests), eval task 09 |
| C5 | Prompt injection in task text, repository files or test output is contained: blocked actions fail the job, canaries never leave, nothing is written outside policy | policy + canary + blocked-action halt | `tests/safety/test_controls.py`; eval tasks 04, 05 (`harness_contained`) |
| C6 | Commands run without network, capabilities or host filesystem access when the container workspace is used | `docker run --network=none --cap-drop=ALL --security-opt no-new-privileges`, worktree-only mount, limits | `tests/unit/test_container_workspace.py` (argv exact-match; real daemon on Linux CI) |
| C7 | Host git never executes repository-controlled hooks or follows a repository-controlled `.git` pointer | `PinnedGit` (`--git-dir`, `core.hooksPath=<empty dir>`), nested repos excluded | `tests/unit/test_workspace_tools.py::test_rewritten_git_pointer_cannot_run_hooks_on_the_host`, `::test_repository_that_is_itself_a_linked_worktree` |
| C8 | Every command and event between Koru and gca is authenticated and bound (method, path, delivery id, body, timestamp, version 2); replays are answered from the record, never re-executed; concurrent duplicates create one job | HMAC v2, reservation rows, lease takeover | `test_sign_and_verify`, `test_inbound_requires_valid_signature`, `tests/unit/test_fuzz.py`, `test_concurrent_duplicate_deliveries_create_one_job`; contract test |
| C9 | The emergency stop is immediate (running command killed ≤ 0.25 s), cannot be undone by a stale delivery, and every change is attributed and reasoned | kill switch + `apply_kill_switch_delivery` one transaction | `test_kill_switch_*`, `test_expired_kill_switch_delivery_cannot_undo_a_newer_stop`, `tests/review_probes/test_recovery.py` |
| C10 | A job never ends `succeeded` if cancelled or stopped during verification | `complete()` decides in the transaction | `test_cancel_during_verification_never_reports_success`, `test_cancel_between_check_and_commit_never_reports_success`, `test_kill_switch_interrupts_verification` |
| C11 | Only the exact verified commit is published; a moved branch or unverified job is refused; every publish names a person | `publish_job` commit pin, `actor` required | `test_publish_pushes_the_verified_commit_not_a_moved_branch`, `test_publish_via_inbound` |
| C12 | Every action is in a hash-chained, append-only audit log that survives concurrent writers and can be verified offline | `AuditLog` + file lock + `verify_chain` | `tests/unit/test_queue_audit.py`, `tests/e2e/test_fault_injection.py` |
| C13 | Secrets (provider keys, webhook secrets, GitHub token) never appear in config files, audit logs, events or the agent's environment; configured canaries are redacted everywhere | `*_env` settings, `redact_event`, `subprocess_env` | `test_canary_secret_never_leaves_via_tool_output_or_diff`, `test_approval_requested_event_is_redacted` |
| C14 | One tenant cannot reach another's repositories or credentials | instance per tenant; `allowed_repos`; per-instance `[publish]` | `tests/unit/test_tenancy.py`; `docs/multi-tenancy.md` |
| C15 | The plain-HTTP server is never exposed off loopback without an operator declaring the TLS proxy | bind guard | `test_plain_http_bind_off_loopback_needs_tls_proxy` |
| C16 | Dependencies are pinned and the same pins are what CI tests | `requirements*.lock` | CI installs from the lock |

All automated evidence runs on every CI build (`.github/workflows/ci.yml`, Ubuntu 3.10/3.12 with Docker and
Windows 3.12). The matrix in `docs/pilot-scenarios.md` maps scenarios to the same tests.

## 3. What the reviewer verifies in the deployment (not provable from this repository)

- [ ] One gca instance per tenant, each as its own OS user with its own `data_dir`, secrets and `allowed_repos`
      (`docs/multi-tenancy.md`).
- [ ] `[workspace] kind = "container"` for every tenant whose repositories are not fully trusted by the host;
      the image is from a trusted registry and patched on a schedule.
- [ ] `inbound_host` is loopback, or a TLS-terminating proxy is the only route in and `behind_tls_proxy` is
      set; the proxy does not rewrite the signed path.
- [ ] Secrets are injected from the platform's secret store (`docs/runbook.md` § 3); no `gca.toml` contains one.
- [ ] The GitHub token is scoped to the tenant's repositories with `contents` and `pull_requests` only.
- [ ] Branch protection on every target repository requires a human review before merge, so a draft PR from
      gca cannot be merged by automation.
- [ ] `canary_strings` is set per tenant and the same strings are planted in the environment the agent can see.
- [ ] `gca prune` runs on a schedule and `.gca/audit/` is archived per the tenant's records policy (§ 7 of
      the runbook); the audit directory is on a filesystem with the repository's classification.
- [ ] Koru's approval policy forbids self-approval at its level too (gca enforces it by name only).
- [ ] The model provider or gateway route is the one approved for this tenant's data classification.

## 4. Residual risks accepted for the pilot

1. **A subtle bad fix that passes tests.** The harness proves the tests pass and nothing was tampered with;
   it does not prove the change is correct. Mitigation: human review of every draft PR; bounded tasks;
   diff-size budgets. (`docs/architecture.md` § Residual risks.)
2. **Data to the model provider.** Code and task text leave the boundary. Mitigation: a self-hosted or
   gateway-routed model for sensitive repositories; `docs/eval-baseline.md` records what local models can do.
3. **Worktree mode** gives allow-listed programs host access. Accepted only on single-developer machines.
4. **Advisory conditions** are not enforced (by design); eval task 11 measures how often a model honours them.
   A condition that must hold is expressed as a restriction or a test.
5. **Identity is the platform's.** gca records the names Koru supplies. A compromised Koru can submit, approve
   (as a different name) and publish within the tenant's `allowed_repos` and the token's scope — but still
   only to draft PRs behind branch protection, and every action is in the audit log.
6. **Licence**: the code is marked `Proprietary` in `pyproject.toml` pending a decision; no third-party code
   with copyleft obligations is vendored (dependencies: pydantic, httpx, tomli — MIT/BSD).

## 5. Open items blocking "integrated" (as opposed to "pilot") sign-off

- Contract test run against the real Koru adapter on staging (`docs/koru-adapter.md` § Acceptance).
- Koru's identity model decision: instance per tenant suffices, or key ids per caller are needed.
- Token broker for per-tenant GitHub credentials (short-lived installation tokens) instead of a PAT.
- A real-model eval run on the current main with tasks 10 and 11 (`docs/pilot-scenarios.md` § Evidence).

## Sign-off

| Role | Name | Date | Decision |
|---|---|---|---|
| Security reviewer | | | pilot / integrated / declined |
| Platform owner (Koru) | | | |
| Tenant owner | | | |
