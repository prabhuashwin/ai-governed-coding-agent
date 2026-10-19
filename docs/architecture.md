# Architecture and threat model

## Components

```
                 ┌──────────────┐       ┌───────────────┐
 submit ───────► │  JobQueue    │ ◄──── │ sources/      │  (ServiceNow Table API, later)
 approve ──────► │  (SQLite)    │       └───────────────┘
                 └──────┬───────┘
                        │ claim_next (atomic)
                 ┌──────▼───────┐
                 │   Worker     │  heartbeat, cancel checks, crash → FAILED
                 └──────┬───────┘
          ┌─────────────┼───────────────────┐
   ┌──────▼──────┐ ┌────▼─────┐ ┌───────────▼─────────┐
   │ Workspace   │ │ Policy   │ │ Provider (adapter)  │
   │ git worktree│ │ Engine   │ │ anthropic / openai… │
   └──────┬──────┘ └────┬─────┘ └───────────┬─────────┘
          └──────┬──────┴─────────┬─────────┘
          ┌──────▼──────┐  ┌──────▼──────┐
          │ToolExecutor │◄─│ AgentLoop   │  observe → reason → act, budgets, compaction
          └─────────────┘  └──────┬──────┘
                                  │ commit on gca/<job>
                          ┌───────▼────────┐
                          │   Verifier     │  fresh checkout · tamper checks · run tests · canary scan
                          └───────┬────────┘
                          ┌───────▼────────┐
                          │ Report + Audit │  PR markdown · hash-chained JSONL
                          └────────────────┘
```

## Job state machine

`submitted → approved → running → verifying → succeeded | failed`, with `rejected` from submitted and
`cancelled` from any non-terminal state. Transitions outside `models.TRANSITIONS` raise. A worker that
dies leaves a stale heartbeat; `recover_stale` moves the job to `failed` with a reason.

## Trust boundaries

| Input | Trust | Handling |
|---|---|---|
| Work item text (title, description, criteria) | Untrusted | Framed as data in the prompt; never interpolated into commands |
| Repository contents, test output, tool results | Untrusted | Same; policy applies regardless of what they say |
| Model output (tool calls) | Untrusted | Every call validated by `PolicyEngine` before execution |
| `gca.toml` (policy, budgets, provider) | Trusted (operator) | Only operators edit it; it is not reachable from the worktree |
| Approval | Trusted (human) | Required before any job runs; recorded in audit |

## Controls and where they are enforced

| Control | Where | Tested in |
|---|---|---|
| Path confinement, protected files, allowed write paths (file tools) | `policy.resolve_path` | unit, safety |
| Every file in the diff re-checked against the same rules, so commands cannot write where the file tools could not | `Verifier.verify` | safety |
| Command allow-list, no shell operators, no absolute/traversal args | `policy.check_command` | unit, safety |
| Secret-free subprocess environment | `policy.subprocess_env` | unit, safety |
| Budgets (turns, tool calls, tokens, cost, time) | `BudgetTracker`, checked every turn and every tool call | unit, loop |
| Blocked-action halt; blocked action ⇒ job fails | `AgentLoop`, `Worker` | loop, safety |
| Canary secrets withheld from output, flagged in diff, redacted in audit | `ToolExecutor`, `Verifier`, `AuditLog` | safety |
| Test tamper detection (deleted/renamed/skipped tests, fewer test definitions, test config modified **or added**) | `Verifier.tamper_flags` | safety |
| Fewer tests *executed* than at the base commit (catches collection-time deselection by any means) | `Verifier.verify`, baseline run | safety |
| Host git never trusts the worktree's `.git` pointer: pinned git dir, empty hooks path, nested repos excluded | `workspace.PinnedGit` | unit |
| Diff-size budget (files / lines changed) | `Verifier.verify` from `Budget` | safety |
| Container isolation (no network, dropped caps, limits) for agent and verifier | `ContainerWorkspace`, `make_runner` | unit (fake runtime), real daemon when present |
| Publishing is explicit and human-invoked; PRs open as drafts; only the verified commit is pushed | `publish.py` | unit |
| Webhook signatures bind method, path, delivery id and body; inbound delivery ids reserved before execution | `webhooks.sign`, `inbound.handle` | unit, fuzz |
| Independent test run from clean checkout | `Verifier.verify` | safety, e2e |
| Hash-chained audit log, appended under a cross-process lock | `AuditLog`, `verify_chain` | unit, e2e |
| Idempotent submit, atomic claim, crash recovery | `JobQueue` | unit |

## Residual risks (pilot)

1. **Subprocess escape (worktree mode)** — allow-listed programs (python, node…) can open sockets or read host
   files. Mitigation: `[workspace] kind = "container"` runs each command and the verifier in a throwaway
   container with `--network=none`, `--cap-drop=ALL`, `no-new-privileges`, memory/CPU/pid limits and only the
   worktree mounted. Residual: the container image itself must be trusted and kept patched.
2. **Prompt injection changing the *fix*** — a hostile file could steer the model into a subtle bad change that
   still passes tests. Mitigation: human review of every PR (the report highlights blocked actions and
   diff size); keep tasks bounded; add diff-size budgets per task.
3. **Provider data handling** — code and issue text leave the boundary when using a hosted model. Mitigation:
   `openai_compatible` against a self-hosted model; the core tracker works with the agent disabled.

## Extending

- **Container backend**: `ContainerWorkspace` + `make_runner` already route agent commands and the verifier
  through `docker run --network=none …`; swap the image per language toolchain.
- **New provider**: subclass nothing — implement `complete(system, messages, tools) -> ModelResponse`.
- **ServiceNow**: `sources/servicenow.py` polls approved records and writes status/report back; approval
  stays in ServiceNow.
