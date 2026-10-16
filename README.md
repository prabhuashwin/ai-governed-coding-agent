# Governed Coding Agent (gca)

A runner for the coding-agent subsystem described in the *ServiceNow Engineering Work Management* proposal:

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

Merge, release and production actions stay with humans. ServiceNow is one *work-item source* behind an
adapter; everything runs locally first.

## Why it is agentic, and why it is governed

| Agentic (the model decides)                                   | Governed (the harness decides)                                   |
|---------------------------------------------------------------|------------------------------------------------------------------|
| Which files to read, what to edit, when to run tests          | Path confinement to the worktree; protected files; allowed paths  |
| How to react to a failing test or an error                    | Command allow-list, no shell, no network, scrubbed environment    |
| When the goal is met (`finish(success=…)`)                    | Budgets: turns, tool calls, tokens, cost, wall-clock             |
|                                                               | Blocked-action halt; any blocked action fails the job            |
|                                                               | Independent verifier: fresh checkout, deleted/skipped-test detection, canary scan |
|                                                               | Append-only hash-chained audit log per job                        |

## Provider-agnostic

One `Provider` interface (`src/gca/providers/base.py`). Adapters:

- `anthropic` — Anthropic Messages API
- `openai_compatible` — OpenAI, Azure OpenAI, Ollama, vLLM, LM Studio, OpenRouter, Groq, Together, Mistral, Gemini (OpenAI endpoint)…
- `scripted` / `replay` — deterministic providers for tests and CI

Switching is a config change; see `gca init` output. Add a provider = one class + one line in `providers/__init__.py`.

## Quick start

```bash
# Python 3.10+ and git required
uv venv && uv pip install -e ".[dev]"      # or: python -m venv .venv && pip install -e ".[dev]"
source .venv/bin/activate                  # Windows: .venv\Scripts\activate

gca init                                   # writes gca.toml — pick a provider, set the API key env var
export ANTHROPIC_API_KEY=...               # or OPENAI_API_KEY, or none for a local Ollama/vLLM server

gca submit --title "Fix mean() off-by-one" \
  --description "mean([2,4,6]) returns 6 instead of 4" \
  --criteria "mean divides by n" --criteria "all tests pass" \
  --repo /path/to/repo --test-cmd "python -m pytest -q" --requester ashwin
gca approve <job_id> --approver lead
gca run --once                             # or: gca run  (long-running worker)
gca report <job_id> --diff                 # the PR description + diff
gca audit <job_id> --verify                # hash-chain check of the audit log
gca publish <job_id>                       # push gca/<job_id> and open a draft PR (GITHUB_TOKEN)
gca servicenow-sync --once                 # poll a ServiceNow table for approved items, write status back
```

Isolation is chosen in `gca.toml`: `[workspace] kind = "worktree"` (default) or `kind = "container"`, which
runs every agent command *and* the verifier inside a throwaway Docker/Podman container with no network,
dropped capabilities and memory/CPU/pid limits, the repository bind-mounted at `/work`.

The agent's work lands on branch `gca/<job_id>` in your repository; your working copy is never touched.

## Testing

Fast suite (no model, no network, ~30 s): `make test` or `pytest`.
Real-model evals: `gca eval` locally, or the manual GitHub workflows **evals (real model)**, **evals (local model, Ollama)**
and **servicenow smoke test** — keys stay in repository secrets. See **docs/how-to-test.md**.

## Layout

```
src/gca/
  models.py      pydantic models, job state machine
  config.py      gca.toml → Config (provider, budget, policy, pricing)
  policy.py      PolicyEngine (paths, commands, env, canaries) + BudgetTracker
  queue.py       SQLite job queue: idempotent submit, atomic claim, stale recovery
  workspace.py   GitWorktreeWorkspace, ContainerWorkspace (docker/podman, no network), factory + runner
  tools.py       tool schemas + ToolExecutor
  agent.py       AgentLoop
  verifier.py    independent verification + tamper detection
  worker.py      claim → run → verify → report
  report.py      PR-style markdown report
  audit.py       hash-chained JSONL audit log
  publish.py     push branch + open GitHub pull request
  providers/     base schema, anthropic, openai_compat, testing (scripted/record/replay)
  sources/       servicenow.py (Table API adapter + sync loop; tested against a mocked API)
  evals.py       golden-task eval runner
  cli.py         `gca`
tests/           unit · loop · safety · e2e
evals/tasks/     golden tasks (small repos with seeded bugs)
docs/            architecture, threat model, how-to-test
```

## Status

Pilot-grade. With `kind = "worktree"`, isolation is a git worktree + allow-listed commands + scrubbed
environment — fine for a trusted developer machine, not a sandbox against a hostile model. Use
`kind = "container"` anywhere else. Still open: a real-model eval baseline (`gca eval` with your key),
validating the ServiceNow adapter against a live instance, and a local-model (`openai_compatible`)
run for customers that forbid external inference.
