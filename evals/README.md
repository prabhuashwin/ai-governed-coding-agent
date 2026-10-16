# Golden-task evals

Each task directory holds `task.json` (title, description, acceptance criteria, test command, budget)
and `repo/` — a small project whose tests currently fail. `gca eval` copies each repo into a temp git
repository and pushes it through the *real* pipeline (queue → agent → verifier → report), so policy,
budgets and the verifier are exercised, not just the model.

```
gca -c gca.toml eval                       # all tasks, one run each
gca -c gca.toml eval --only slugify        # one task
gca -c gca.toml eval --repeats 3           # variance: same task three times
```

Results land in `evals/results/<timestamp>.json` and `.md` (pass rate, turns, tool calls, tokens,
cost, seconds, lines changed, flags). Keep the `.md` files in version control to track regressions
across prompt, provider or model changes.

Adding a task: copy a directory, change the seeded bug and tests, keep the repo tiny (seconds to test).
Good tasks have one clear failing behaviour and tests that cannot be satisfied by deleting them —
the verifier rejects that anyway, but it keeps the eval honest.
