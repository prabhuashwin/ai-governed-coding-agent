# Governed Coding Agent (gca)

gca turns a ticket into a reviewed, tested code change, with an audit trail to show for it. An engineer
submits a work item with acceptance criteria and someone approves it. An AI coding agent then makes the change
in an isolated copy of the repository, and **independent code re-runs the tests** before anyone is asked to
review it. A human always decides whether to merge.

It is the coding-agent subsystem described in the *ServiceNow Engineering Work Management* proposal. It runs
locally on its own, and connects to ServiceNow (work items and approvals), GitHub (pull requests) and
agent-operations platforms such as Koru (webhooks).

```
engineer submits work item ──► approver authorizes ──► worker claims job
      │                                                      │
      │                               isolated git worktree on branch gca/<job>
      │                                                      │
      │                        agent loop: model picks tools; harness enforces policy & budget
      │                                                      │
      │                        commit ──► INDEPENDENT VERIFIER (clean checkout, tamper checks, tests)
      │                                                      │
      └────────────── report (PR description) + diff + test evidence + cost + hash-chained audit log
```

**Contents:** [Why agentic and governed](#why-it-is-agentic-and-why-it-is-governed) ·
[Install](#install) · [First run](#first-run-five-minutes) · [Configuration](#configuration) ·
[Integrations](#integrations) · [Command reference](#command-reference) · [Testing](#testing) ·
[Documentation](#documentation) · [Layout](#layout) · [Status](#status-and-limitations)

## Why it is agentic, and why it is governed

| Agentic (the model decides)                                   | Governed (the harness decides)                                   |
|---------------------------------------------------------------|------------------------------------------------------------------|
| Which files to read, what to edit, when to run tests          | Path confinement to the worktree; protected files; allowed paths  |
| How to react to a failing test or an error                    | Command allow-list, no shell, no network, scrubbed environment    |
| When the goal is met (`finish(success=…)`)                    | Budgets: turns, tool calls, tokens, cost, wall-clock, diff size  |
|                                                               | Any blocked action fails the job; kill switch and cancel interrupt a command in flight |
|                                                               | Approvers can narrow paths, commands and budget per job (enforced) |
|                                                               | Independent verifier: fresh checkout, deleted/skipped-test detection, canary scan |
|                                                               | Append-only, hash-chained audit log per job                       |

## Install

Requirements: **Python 3.10+** and **git**. Docker or Podman is optional (container isolation).

**Windows (PowerShell)**
```powershell
git clone https://github.com/prabhuashwin/ai-governed-coding-agent.git
cd ai-governed-coding-agent
python -m venv .venv
.venv\Scripts\activate
pip install -e ".[dev]"
gca --version
```

**macOS / Linux**
```bash
git clone https://github.com/prabhuashwin/ai-governed-coding-agent.git
cd ai-governed-coding-agent
python3 -m venv .venv && source .venv/bin/activate      # or: uv venv && uv pip install -e ".[dev]"
pip install -e ".[dev]"
gca --version
```

Check the install with `pytest -q`. It takes about two minutes and needs no API key or network.

## First run (five minutes)

1. **Create a config and choose a model provider.**
   ```bash
   gca init                 # writes gca.toml with commented examples for every provider
   ```
   Set the API key as an environment variable. Never put it in a file:
   ```powershell
   $env:OPENAI_API_KEY = "sk-..."          # PowerShell
   ```
   ```bash
   export ANTHROPIC_API_KEY=sk-ant-...     # bash/zsh
   ```
   In `gca.toml`, set `[provider]` to match. For example, OpenAI's small model:
   ```toml
   [provider]
   kind = "openai_compatible"
   model = "gpt-4.1-mini"
   api_key_env = "OPENAI_API_KEY"
   ```

2. **Submit a work item** against any git repository with a test command:
   ```bash
   gca submit --title "Fix mean() off-by-one" \
     --description "mean([2,4,6]) returns 6 instead of 4" \
     --criteria "mean() returns the arithmetic mean" --criteria "all tests pass" \
     --repo /path/to/repo --test-cmd "python -m pytest -q" --requester alice
   ```
   This prints a job id such as `job_3f2a…`. To try it on a ready-made example, copy one of the
   repositories under `evals/tasks/*/repo` **outside this clone**, then run
   `git init && git add -A && git commit -m init` inside the copy and point `--repo` at it.

3. **Approve it, then run it.**
   ```bash
   gca approve job_3f2a… --approver bob
   gca run --once
   ```
   This prints `job_3f2a… -> succeeded`, or `failed` with the reason.

4. **Review the result.**
   ```bash
   gca report job_3f2a… --diff        # verdict, test evidence, cost, agent summary, diff
   gca audit  job_3f2a… --verify      # proves the audit log has not been edited
   ```
   The change is on branch `gca/job_3f2a…` in your repository. Your working copy is never touched. See
   [`docs/sample-report.md`](docs/sample-report.md) for what a report looks like.

5. **Optional: open a pull request.** Set `GITHUB_TOKEN`, then run `gca publish job_3f2a…`. This pushes the
   branch and opens a *draft* PR with the report as its description.

A bounded fix costs about $0.005 on `gpt-4.1-mini` (see [`docs/eval-baseline.md`](docs/eval-baseline.md)).

## Configuration

Everything is in one `gca.toml`. `gca init` writes a documented example; a full one is in
[`gca.example.toml`](gca.example.toml).

| Section | What it controls |
|---|---|
| `[provider]` | Model and endpoint. `anthropic`, or `openai_compatible` for OpenAI, Azure, Ollama, vLLM, OpenRouter, Groq… or an AI gateway that speaks the OpenAI API |
| `[budget]` | Hard caps per job: turns, tool calls, tokens, USD cost, seconds, files and lines changed |
| `[policy]` | Commands the agent may run, protected paths, secret canaries, blocked-action limit |
| `[workspace]` | `kind = "worktree"` (default; for a trusted machine) or `"container"`: every command *and* the verifier run in a throwaway Docker/Podman container with no network, dropped capabilities and resource limits |
| `[approval]` | `mode = "local"` (`gca approve`), or `"webhook"`, where decisions arrive only from an external platform |
| `[webhooks]` | Signed events out and signed commands in. See [Integrations](#integrations) |
| `[servicenow]` | Instance, table and field prefix for `gca servicenow-sync` |
| `[pricing]` | USD per million tokens by model, used for cost reporting and the cost budget |

Secrets are always referenced by **environment variable name** (`api_key_env`, `outbound_secret_env`, …) and
never stored in `gca.toml`. `gca.toml` is git-ignored as well.

## Integrations

### Agent-operations platforms (Koru, or any webhook-capable system)

gca and the platform exchange **HMAC-signed JSON over HTTPS**. Neither side needs the other's SDK.

| Direction | What flows |
|---|---|
| gca → platform (events) | `job.submitted`, `approval.requested`, `approval.decided`, `job.started`, `job.blocked_action`, `job.verified`, `job.finished` (status, branch, model, tokens, cost, verification, report, audit hash), `job.cancel_requested`, `job.published`, `control.kill_switch` |
| platform → gca (commands) | `POST /v1/work-items` (submit), `POST /v1/jobs/{id}/decision` (approve/reject, with enforced restrictions), `POST /v1/jobs/{id}/cancel`, `POST /v1/jobs/{id}/publish`, `POST /v1/controls/kill-switch` (global/tenant/job scope), `GET /v1/jobs/{id}` |

Minimal setup:

```toml
[approval]
mode = "webhook"                                  # the platform approves; gca approve is refused

[webhooks]
enabled = true
outbound_urls = ["https://koru.example.com/hooks/gca"]
outbound_secret_env = "GCA_WEBHOOK_SECRET"        # shared secret: signs events gca sends
inbound_secret_env  = "GCA_INBOUND_SECRET"        # shared secret: verifies commands gca receives
inbound_port = 8787
```

```bash
export GCA_WEBHOOK_SECRET=... GCA_INBOUND_SECRET=...
gca webhook-serve --with-worker     # accepts commands, runs jobs, delivers events every 5 s
gca webhook-status                  # outbound deliveries: pending / delivered / dead
gca kill on --reason "incident" --by secops    # stop everything; `gca kill off --reason ...` to resume
```

What you can rely on:
- **Delivery is durable and at least once.** Events wait in a local outbox and are retried with backoff, so
  a receiver outage never fails a job.
- **Inbound commands are idempotent** per delivery id.
- **Self-approval is refused**, and a caller can lower a job's budget but never raise it.
- **The kill switch** stops running agents at their next turn, kills a command or verifier test run in flight
  within about a second, and blocks new work. Turning it off requires a reason. `scope = "job"` stops one job.
- **Approvals can restrict**: structured `restrictions` (paths, commands, budget) can only narrow what the
  work item and operator policy allow, and are enforced, not advisory. Free-text `conditions` are shown to
  the reviewer.
- **Publishing can be requested by the platform** (`POST /v1/jobs/{id}/publish`, attributed to a person) or
  switched off with `allow_publish = false`.
- **One instance per tenant**: its own secrets, `[policy] allowed_repos`, `[publish]` token and data
  directory; the plain-HTTP server only binds off loopback behind a declared TLS proxy. See
  [`docs/multi-tenancy.md`](docs/multi-tenancy.md).

The full contract is in **[`docs/webhooks.md`](docs/webhooks.md)**: headers, signing, every payload,
verification code in Python and Node, and how Koru's approvals, kill switches, observability and AI gateway
map onto it. Hand it to whoever owns the platform side.

### ServiceNow

`gca servicenow-sync` polls a ServiceNow table for records in state `approved`. It queues each one, mirroring
the ServiceNow approver into gca's approval and audit log, and writes the status, branch, report and audit
hash back onto the record. Approval always happens in ServiceNow. `scripts/servicenow_bootstrap.py` creates
the table on a developer instance, and the **servicenow smoke test** workflow runs the whole round trip.

### GitHub

`gca publish <job>` pushes the job's branch and opens a draft pull request whose description is the
verification report. Without `GITHUB_TOKEN`, it pushes the branch only.

## Command reference

| Command | Purpose |
|---|---|
| `gca init` | Write an example `gca.toml` |
| `gca submit …` | Submit a work item (`--title --description --criteria --repo --test-cmd --allowed-path --max-cost --max-files …`) |
| `gca approve <job> --approver X [--reject] [--override] [--allow-path P] [--allow-command C] [--max-cost … --max-turns … --max-files … --max-lines …]` | Approve or reject locally, optionally narrowing what the job may touch, run and spend (`--override` is break-glass in webhook mode, and is audited) |
| `gca run [--once]` | Run the worker (one job, or continuously) |
| `gca status [job]` | List jobs, or show one in full |
| `gca report <job> [--diff] [--out f.md]` | The review report (PR description) |
| `gca audit <job> [--verify]` | Show the audit trail, or verify its hash chain |
| `gca cancel <job>` | Cancel queued work, or stop a running agent (a command in flight is killed) |
| `gca publish <job>` | Push the branch and open a draft GitHub PR |
| `gca webhook-serve [--with-worker]` | Serve signed inbound commands and deliver outbound events |
| `gca webhook-deliver [--requeue-dead]` / `gca webhook-status` | Flush or inspect outbound deliveries |
| `gca prune [--dry-run]` | Apply `[retention]`: drop old terminal jobs, delivered/dead events and inbound idempotency records (audit logs are kept) |
| `gca kill on\|off\|status` | Global kill switch |
| `gca servicenow-sync [--once]` | Poll ServiceNow for approved work and write results back |
| `gca eval [--repeats N] [--only name]` | Run the golden-task evals against the configured model |

Every command accepts `-c path/to/gca.toml` (default `./gca.toml`, or `$GCA_CONFIG`).

## Testing

| Layer | Command | Needs |
|---|---|---|
| Unit, loop, safety (red-team), end-to-end: 310 tests | `pytest -q` | nothing; about 2 minutes |
| Real containers | `GCA_TEST_RUNTIME=docker GCA_TEST_IMAGE=<image with pytest> pytest tests/unit/test_container_workspace.py` | Docker or Podman |
| Golden-task evals, real model | `gca eval`, or the **evals (real model)** GitHub workflow | an API key |
| Local model (no external inference) | **evals (local model, Ollama)** workflow | nothing |
| ServiceNow round trip | **servicenow smoke test** workflow | a ServiceNow developer instance |

CI runs the full suite on Python 3.10 and 3.12 on every push. The evals and smoke test are manual workflows;
keys stay in repository secrets, and results are committed to the `eval-results` branch. Details are in
[`docs/how-to-test.md`](docs/how-to-test.md).

## Documentation

| Document | For |
|---|---|
| [`docs/architecture.md`](docs/architecture.md) | Components, job state machine, trust boundaries, threat model |
| [`docs/how-to-test.md`](docs/how-to-test.md) | Every test layer and how to extend it |
| [`docs/eval-baseline.md`](docs/eval-baseline.md) | Measured pass rates and costs (hosted and local models) and what they mean |
| [`docs/webhooks.md`](docs/webhooks.md) | The integration contract for Koru or any webhook platform |
| [`docs/multi-tenancy.md`](docs/multi-tenancy.md) | One instance per tenant: what is per instance, identity trust, TLS placement, operating N instances |
| [`docs/koru-adapter.md`](docs/koru-adapter.md), [`docs/webhook-contract-samples.json`](docs/webhook-contract-samples.json) | Who owns the Koru-side adapter, what it maps, and real samples of every command and event to build it against |
| [`docs/pilot-scenarios.md`](docs/pilot-scenarios.md) | The pilot scenario matrix: every scenario mapped to its automated evidence and real-model evidence, the evidence format, and the pilot operating system |
| [`docs/runbook.md`](docs/runbook.md) | Operator runbook: install, configure, credentials, run, upgrade and migration, recovery, retention, dependencies, troubleshooting |
| [`docs/security-signoff.md`](docs/security-signoff.md) | Security sign-off for an integrated deployment: claims with evidence, deployment checklist, residual risks, open items |
| [`docs/sample-report.md`](docs/sample-report.md), [`docs/sample-audit.txt`](docs/sample-audit.txt) | What a finished job produces |
| [`evals/README.md`](evals/README.md) | How golden tasks work and how to add one |

## Layout

```
src/gca/
  models.py      pydantic models, job state machine
  config.py      gca.toml → Config
  policy.py      PolicyEngine (paths, commands, env, canaries) + BudgetTracker
  queue.py       SQLite job queue: idempotent submit, atomic claim, stale recovery, kill switch
  workspace.py   GitWorktreeWorkspace, ContainerWorkspace (docker/podman, no network)
  tools.py       tool schemas + ToolExecutor
  agent.py       AgentLoop
  verifier.py    independent verification + tamper detection
  worker.py      claim → run → verify → report
  report.py      PR-style markdown report
  audit.py       hash-chained JSONL audit log
  publish.py     push branch + open GitHub pull request
  webhooks.py    signed outbound events: outbox, retries, dead letters
  inbound.py     signed inbound commands: submit / decision / cancel / publish / kill switch (HTTP server)
  providers/     anthropic, openai_compat, testing (scripted/record/replay)
  sources/       servicenow.py (Table API adapter + sync loop)
  evals.py       golden-task eval runner
  cli.py         `gca`
tests/           unit · loop · safety · e2e
evals/tasks/     golden tasks (small repos with seeded bugs)
scripts/         servicenow_bootstrap.py, smoke_script.json
docs/            see Documentation above
.github/         ci.yml, evals.yml, evals-local.yml, servicenow-smoke.yml
```

## Status and limitations

Pilot-grade, with a measured baseline ([`docs/eval-baseline.md`](docs/eval-baseline.md)):
- Hosted small models pass **15/15** verified golden tasks at about $0.005 per task.
- A 7B local model passes 1/3, and the harness contained every failure mode it produced.

Limitations to know before relying on it:
- **Isolation.** `kind = "worktree"` is fine on a trusted developer machine but is not a sandbox against a
  hostile model. Use `kind = "container"` anywhere else; it is validated on Podman and Docker.
- **Webhooks.** Tested against a simulated platform; not yet connected to Koru itself, which needs its
  endpoint and shared secrets.
- **ServiceNow.** Tested against a mocked API; the live round trip needs a developer instance.
- **Merging.** Merge, release and production actions are deliberately out of scope and stay with humans.

## Licence and release

The code is marked `Proprietary` in `pyproject.toml` until the owners decide otherwise; it is not licensed
for redistribution. Releases are git tags `v<version>` (`CHANGELOG.md`); the `v0.2.0` tag exists in the
maintainer's local checkout and must be pushed from there (`git push origin v0.2.0`).
