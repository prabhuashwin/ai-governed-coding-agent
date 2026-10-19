# Operator runbook

For whoever runs a gca instance for a tenant. Design and threat model are in `docs/architecture.md`; the
tenancy model in `docs/multi-tenancy.md`; the webhook contract in `docs/webhooks.md`. This document is the
"how do I" for day-to-day operation.

## 1. Install

Pilot host: Linux (Ubuntu 22.04+ or WSL2), Python 3.10–3.12 (the test suite needs ≥ 3.10.2), git, and Docker
or Podman for the container workspace. One OS user per tenant instance.

```bash
python -m venv /opt/gca/tenant-a/venv && . /opt/gca/tenant-a/venv/bin/activate
pip install -r requirements.lock && pip install --no-deps .      # pinned; see § Dependencies
gca init > /etc/gca/tenant-a.toml                                # then edit
```

Verify: `gca --version`, `gca -c /etc/gca/tenant-a.toml status` (creates the data dir), and for container
mode `docker run --rm python:3.12-slim python -c 'print(1)'` as the gca user.

## 2. Configure (`gca.toml`)

The minimum for a platform-driven tenant instance:

```toml
data_dir = "/var/lib/gca/tenant-a"          # queue, outbox, audit logs; one per instance
worker_id = "tenant-a-1"

[provider]                                  # or an OpenAI-compatible gateway route for this tenant
kind = "anthropic"; model = "claude-sonnet-4-5"; api_key_env = "ANTHROPIC_API_KEY"

[workspace]
kind = "container"                          # worktree mode only for repositories the host already trusts
image = "python:3.12-slim"                  # must contain the tenant's test toolchain (pytest, node …)

[policy]
allowed_repos = ["/srv/repos/tenant-a"]     # REQUIRED on shared or platform-driven hosts
allowed_commands = ["python", "python3", "pytest"]
canary_strings = ["GCA-CANARY-<random>"]    # plant the same string in the environment to detect exfiltration

[approval]
mode = "webhook"

[webhooks]
enabled = true
outbound_urls = ["https://koru.example/hooks/gca/tenant-a"]
inbound_host = "127.0.0.1"; inbound_port = 8781
allow_publish = true

[publish]
remote = "origin"; draft = true; token_env = "GITHUB_TOKEN"

[retention]
jobs_days = 90; outbox_days = 14; inbound_days = 14
```

Every `*_env` setting names an environment variable; the file never holds a secret. `gca init` prints a
fully commented example.

## 3. Credentials

| Variable | Holds | Scope |
|---|---|---|
| `ANTHROPIC_API_KEY` / `OPENAI_API_KEY` (per `[provider] api_key_env`) | model access | this tenant's gateway route or key; the agent never sees it (scrubbed from subprocess environments) |
| `GCA_INBOUND_SECRET` | verifies signed commands from the platform | one per instance; rotate by deploying the new value to both sides and restarting (`max_skew_seconds` gives no overlap — do it in a quiet minute, or run two instances briefly) |
| `GCA_WEBHOOK_SECRET` | signs events to the platform | one per instance; same rotation |
| `GITHUB_TOKEN` (per `[publish] token_env`) | pushes branches and opens draft PRs | a token limited to this tenant's repositories; `contents: write`, `pull_requests: write`; prefer a GitHub App installation token refreshed by the platform |
| `SN_USER` / `SN_PASSWORD` | ServiceNow source (optional) | a service account on the gca table only |

Secrets come from the platform's secret store into the service's environment (systemd `EnvironmentFile=`
with mode 0600, or the orchestrator's secret mount). Never in `gca.toml`, never in the repository.

## 4. Run

```bash
# platform-driven tenant: inbound server + worker + outbound delivery in one process
gca -c /etc/gca/tenant-a.toml webhook-serve --with-worker --poll 2 --flush-every 5
# local/ServiceNow-driven: the worker alone
gca -c /etc/gca/tenant-a.toml run
gca -c /etc/gca/tenant-a.toml servicenow-sync
```

A systemd unit per instance (`Restart=on-failure`, `User=gca-tenant-a`, `EnvironmentFile=`), the inbound
port reachable only from the TLS proxy (`docs/multi-tenancy.md` § TLS placement). Health:
`GET /healthz` → `{"ok": true, "kill_switch": false}`.

Scheduled: `gca prune` daily (cron or a systemd timer) and an archive of `.gca/audit/` per your records
policy (§ 7).

## 5. Deploy and upgrade

1. Read `CHANGELOG.md` → "Behaviour changes operators must know" for the target version. Signature or
   contract changes need the platform adapter updated in the same window.
2. Stop the instance (`systemctl stop`). Running jobs are stopped at their next turn; a command in flight is
   killed; the job is recorded `failed`/`cancelled` and can be resubmitted. Or engage the kill switch first
   (`gca kill on --reason upgrade --by <you>`), wait for `gca status` to show no `running`/`verifying`, then stop.
3. Install the new version into the venv (pinned: `pip install -r requirements.lock && pip install --no-deps .`).
4. Start. **Schema migration is automatic and additive** (`add_column_if_missing` on first open; WAL mode
   is set on first open). It is safe to start the new version against the old data directory; downgrading
   afterwards is also safe (older versions ignore the added columns).
5. Disengage the kill switch with a reason. Check `/healthz` and `gca webhook-status` (nothing `dead`).

Upgrade one tenant first; watch one job through `job.finished`; then the rest.

## 6. Recovery

| Symptom | What happened | Do |
|---|---|---|
| Job stuck in `running` with no progress | worker died (host reboot, OOM) | On the next start `recover_stale` fails any job whose heartbeat is older than `stale_heartbeat_seconds` (default 600) and removes its orphan worktree; resubmit the work item |
| `gca webhook-status` shows `dead` rows | the platform answered a non-retryable 4xx | fix the receiver, then `gca webhook-deliver --requeue-dead` |
| `webhook_spill.jsonl` in the data dir | the outbox table could not be written | `gca webhook-deliver` drains it; investigate disk/locks |
| `database is locked` on start | two processes opened a fresh database at once | startup retries for 10 s; if persistent, check for a stuck process holding `jobs.sqlite3` |
| Orphan `gca-<job id>-*` worktree directories | crash before cleanup | `recover_stale` removes the ones it knows; otherwise `git worktree prune` in the repository, then delete the directory |
| Platform retries an old command | within `inbound_days` it is answered from the record (`replayed: true`); after that it would run again | keep `inbound_days` above the platform's retry horizon |
| Kill switch "on" but nobody remembers why | | `gca kill status`; the actor and reason are in `.gca/audit/_controls.jsonl`; disengage with `gca kill off --reason "…" --by <you>` |
| Audit log suspected tampered | | `gca audit <job> --verify` recomputes the hash chain; a break names the line |
| Need to undo a job's branch | | `git branch -D gca/<job id>` in the repository; the audit log and report keep the record |

A terminal job cannot be restarted; submit the work item again (a new idempotency key, or the same one
after the old job is pruned).

## 7. Retention and records

`gca prune` applies `[retention]`: terminal job rows after `jobs_days`, delivered/dead outbound rows and
inbound idempotency records after their windows. It never deletes files. What stays, and who owns it:

| Data | Where | Retention |
|---|---|---|
| Audit logs (hash-chained, per job + `_controls`) | `<data_dir>/audit/*.jsonl` | **kept by gca indefinitely**; archive to the tenant's records store on your schedule (they are append-only files; copy and verify with `gca audit <job> --verify` before deleting the originals) |
| Job rows (work item, result, report) | `jobs.sqlite3` | `jobs_days` (default 90) |
| Outbound events | `jobs.sqlite3` (`webhook_outbox`) | `outbox_days` (14); the event content is also in the audit log |
| Inbound idempotency records | `jobs.sqlite3` (`inbound_deliveries`) | `inbound_days` (14) |
| Job branches `gca/<job id>` | the tenant's repository | operator's branch policy; published ones live on in the PR |
| Worktrees | the system temp dir (`$TMPDIR`), `gca-<job id>-*` | removed at job end; orphans on recovery. Give the gca user a private `TMPDIR` on the same filesystem as the repositories |
| Eval results | `evals/results`, `eval-results` branch | kept (evidence) |

Audit logs can contain file paths, command lines and tool output excerpts from the tenant's repository,
redacted only for configured canary strings. Treat them with the repository's own classification.

## 8. Dependencies

Runtime dependencies are pinned in `requirements.lock`; test/dev tools in `requirements-dev.lock`; CI
installs from the dev lock. To upgrade:

```bash
python -m venv /tmp/relock && . /tmp/relock/bin/activate
pip install -e ".[dev]"                      # resolves the ranges in pyproject.toml
pip freeze | grep -v governed-coding-agent   # paste into the two lock files (keep the tomli marker)
pytest -q                                    # the full suite must pass before the lock is committed
```

Pins are reviewed with each release; `CHANGELOG.md` notes dependency bumps that change behaviour.

## 9. Troubleshooting

- **Agent cannot run the test command**: the container image lacks the toolchain, or the command is not in
  `allowed_commands`. The report says which; `gca audit <job>` shows the blocked call.
- **Every job fails verification with `write_outside_policy`**: the work item's `allowed_paths` or an
  approval restriction is narrower than the fix needs. Widen the work item (not the restriction) and resubmit.
- **`restrictions rejected`** on approval: the approver tried to widen scope, commands or budget; only
  narrowing is possible.
- **403 on `/v1/work-items`**: `repo_path` is outside `allowed_repos`. Check the symlink-resolved path.
- **Refusing to bind**: `inbound_host` is not loopback and `behind_tls_proxy` is false.
- **Signature failures** (`401`): clock skew beyond `max_skew_seconds`, a proxy that rewrites the path, or
  mismatched secrets. The receiver's log says which check failed.
- **Costs higher than expected**: `[budget] max_cost_usd` caps each job; `job.finished` carries the usage;
  `gca status <job>` shows it; the eval baseline gives expected per-task costs for each model.
