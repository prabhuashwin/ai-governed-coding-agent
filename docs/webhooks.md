# Webhook integration (e.g. Koru)

gca talks to an external agent-operations platform over two signed HTTP channels. Neither side needs the
other's SDK, only this contract.

```
             outbound events (gca → platform)                inbound commands (platform → gca)
 gca ───────────────────────────────────────────►  Koru  ─────────────────────────────────────► gca
   job.submitted, approval.requested, approval.decided,        POST /v1/work-items
   job.started, job.blocked_action, job.verified,              POST /v1/jobs/{id}/decision
   job.finished, job.cancel_requested, job.published,          POST /v1/jobs/{id}/cancel
   control.kill_switch                                         POST /v1/jobs/{id}/publish
                                                               POST /v1/controls/kill-switch
                                                               GET  /v1/jobs/{id}
```

Real samples of every command (request and response) and every event, produced by the cross-process
contract test, are in **`docs/webhook-contract-samples.json`**. Who builds the Koru side and how the two
stay in step: `docs/koru-adapter.md`.

## Mapping to Koru capabilities

| Koru capability | Wiring |
|---|---|
| Approval operations (risk, routing, SLA, delegate, conditions) | `approval.mode = "webhook"`. gca emits `approval.requested`; Koru routes it through its approval policy and POSTs the decision back. Free-text `conditions` are recorded on the approval, in the audit log and in the report (advisory); structured `restrictions` (paths, commands, budget) are **enforced** by the policy engine, the agent loop and the verifier. Self-approval is refused by gca as well |
| Emergency containment (kill switch) | Koru POSTs `/v1/controls/kill-switch` with a `scope`. `global`/`tenant` stop this instance: running agents stop at their next turn or, if a command or the verifier's test run is in flight, within about a second (`stop_reason = kill_switch`, job `cancelled`, `error = kill_switch`); nothing new is claimed, and disengaging requires a reason. `job` stops one job (it maps onto cancellation) |
| Publishing | After `job.finished` with `status = succeeded`, Koru POSTs `/v1/jobs/{id}/publish` naming the human who asked; gca pushes the verified commit and opens a draft PR, emitting `job.published`. Operators can turn this off with `allow_publish = false` |
| Observability & economics | `job.started`, `job.verified` and `job.finished` (status, model, tokens, cost, verification, report, audit tail hash) feed Koru's per-agent activity and cost views |
| Channels (Portal, Slack, Teams) | A Koru agent or skill POSTs `/v1/work-items`; the result returns as `job.finished` with the branch and PR-ready report |
| AI gateway | Independent of webhooks. If the gateway exposes an OpenAI-compatible endpoint, set `[provider] kind = "openai_compatible"` and `base_url` to it, and model routing and spend then appear in Koru |

## Configuration

```toml
[approval]
mode = "webhook"                       # decisions only via signed POST /v1/jobs/<id>/decision

[webhooks]
enabled = true
outbound_urls = ["https://koru.example.com/hooks/gca"]
outbound_secret_env = "GCA_WEBHOOK_SECRET"   # shared with Koru; signs events gca sends
inbound_secret_env  = "GCA_INBOUND_SECRET"   # shared with Koru; verifies commands gca receives
inbound_host = "0.0.0.0"                     # default 127.0.0.1; any other bind needs behind_tls_proxy = true
behind_tls_proxy = true                      # you have put a TLS-terminating proxy in front (docs/multi-tenancy.md)
inbound_port = 8787
events = []                                  # empty = all; e.g. ["approval.requested", "job.finished"]
include_report = true
max_attempts = 8
allow_publish = true                         # false: POST /v1/jobs/{id}/publish answers 403; use `gca publish`

[publish]                                    # this instance's publishing identity (one tenant = one token)
remote = "origin"
draft = true
token_env = "GITHUB_TOKEN"
# base = "main"; api_base = "https://ghe.example/api/v3"

[policy]
allowed_repos = ["/srv/repos/tenant-a"]      # submissions outside these roots are refused (403)
```

Run it:

```bash
export GCA_WEBHOOK_SECRET=... GCA_INBOUND_SECRET=...
gca webhook-serve --with-worker          # inbound server + worker + outbound delivery every 5 s
gca webhook-status                       # outbound deliveries: pending / delivered / dead
gca webhook-deliver [--requeue-dead]     # flush now
gca kill on|off|status --reason ... --by ...
```

The inbound server is plain HTTP. Put it behind a TLS-terminating reverse proxy or the platform's private
network; never expose it directly to the internet.

## Signing (both directions)

| Header | Value |
|---|---|
| `X-GCA-Timestamp` | Unix seconds |
| `X-GCA-Signature-Version` | `2` |
| `X-GCA-Signature` | `sha256=` + hex HMAC-SHA256(secret, signing string below) |
| `X-GCA-Delivery-Id` | Unique per delivery. Outbound: the event id. Inbound: chosen by the caller, **required** for POST, and used for idempotency (a repeat returns the original response with `"replayed": true`; a copy arriving while the first is still being processed waits up to 5 s for that response) |
| `X-GCA-Event` | Outbound only: the event type |

The signing string binds everything that gives a request its meaning, so a signed request cannot be replayed
as a different operation, against a different job, or under a different delivery id:

```
"2" LF <timestamp> LF <METHOD> LF <path> LF <delivery id> LF <raw body>
```

`METHOD` is upper case. `path` is the request path only: no scheme, host or query string, `/` when empty.
If a proxy in front of the receiver rewrites paths, verify against the path as the sender addressed it.
For a GET the body is empty and the delivery id may be empty, but both separators are still present.
Receivers must reject timestamps more than 300 s off (configurable) and compare signatures in constant time.

Version 1 (`"<timestamp>.<raw body>"`, no version header) is no longer accepted: it let a signed status GET
be replayed as a cancel POST, and a signed approval be redirected to another job.

Verifying a gca event (Python):

```python
import hmac, hashlib, time
def verify(secret: str, method: str, path: str, headers, body: bytes) -> bool:
    ts, sig = headers["X-GCA-Timestamp"], headers["X-GCA-Signature"]
    if headers.get("X-GCA-Signature-Version", "2") != "2" or abs(time.time() - int(ts)) > 300:
        return False
    head = "\n".join(["2", ts, method.upper(), path, headers.get("X-GCA-Delivery-Id", "")]) + "\n"
    mac = hmac.new(secret.encode(), head.encode() + body, hashlib.sha256).hexdigest()
    return hmac.compare_digest("sha256=" + mac, sig)
```

Node:

```js
const crypto = require("crypto");
function verify(secret, method, path, headers, rawBody) {
  const ts = headers["x-gca-timestamp"], sig = headers["x-gca-signature"];
  if ((headers["x-gca-signature-version"] ?? "2") !== "2" || Math.abs(Date.now() / 1000 - Number(ts)) > 300) return false;
  const head = ["2", ts, method.toUpperCase(), path, headers["x-gca-delivery-id"] ?? ""].join("\n") + "\n";
  const mac = crypto.createHmac("sha256", secret).update(head).update(rawBody).digest("hex");
  const a = Buffer.from(`sha256=${mac}`), b = Buffer.from(sig ?? "");
  return a.length === b.length && crypto.timingSafeEqual(a, b);
}
```

Signing a command to gca is the same computation with the inbound secret, the method and path you are
calling, and the delivery id you chose. `gca.webhooks.signed_headers(secret, method, url, body, delivery_id=…)`
produces the headers if the caller is Python.

## Outbound events

Envelope (every event):

```json
{"id": "evt_…", "type": "job.finished", "created_at": "2026-10-18T07:12:03+00:00",
 "source": "gca", "job_id": "job_…", "audit_seq": 21, "data": { … }}
```

| Type | `data` |
|---|---|
| `job.submitted` | `work_item` {id, title, description, acceptance_criteria, repo_path, base_ref, allowed_paths, requester, source, source_ref}, `budget` |
| `approval.requested` | title, description, acceptance_criteria, requester, repo_path, allowed_paths, budget, source, source_ref, `decide` {method, path, body template}. Emitted only when `approval.mode = "webhook"` |
| `approval.decided` | decision (`approve` or `reject`), approver, note, conditions[], restrictions (object or null) |
| `job.started` | worker, attempt, branch, title |
| `job.blocked_action` | tool, args (truncated), reason |
| `job.verified` | passed, flags[], exit_code, files_changed[] |
| `job.finished` | status, error, audit_tail_hash, title, branch, provider, model, requester, approver, source, source_ref, `agent` {stop_reason, claimed_success, blocked_actions, usage}, `verification` {passed, flags, exit_code, files_changed, insertions, deletions}, report_markdown (optional) |
| `job.cancel_requested` | by, scope (`job`), reason |
| `job.published` | step (`branch_pushed` or `pull_request_opened`), actor, branch, remote, commit, url, base, draft |
| `control.kill_switch` | enabled, reason, actor, scope (`global` or `tenant`; `job_id` is null) |

Delivery is **at least once**: deduplicate on `id`. Order is creation order per process, but receivers
should not depend on it. Any configured canary secret is replaced with `[REDACTED]` before an event leaves.

## Inbound commands

### `POST /v1/work-items` → 201
```json
{"title": "Fix mean() off-by-one", "description": "…", "acceptance_criteria": ["…"],
 "repo_path": "/srv/repos/calc", "base_ref": "main", "test_command": ["python", "-m", "pytest", "-q"],
 "allowed_paths": ["src"], "requester": "alice@corp", "source": "koru", "source_ref": "koru-req-123",
 "idempotency_key": "koru:req-123", "budget": {"max_turns": 15, "max_cost_usd": 0.25}}
```
Response: `{"job_id": "job_…", "status": "submitted", "approval": "pending_webhook_decision"}`.
403 when `repo_path` is not inside one of the instance's `[policy] allowed_repos`.
`budget` can only **lower** the operator's configured limits.

### `POST /v1/jobs/{id}/decision` → 200
```json
{"decision": "approve", "approver": "bob@corp", "note": "…",
 "conditions": ["no API changes"],
 "restrictions": {"allowed_paths": ["src/stats"], "allowed_commands": ["pytest"],
                  "budget": {"max_turns": 10, "max_cost_usd": 0.50, "max_files_changed": 3, "max_lines_changed": 80, "max_seconds": 600}}}
```
`conditions` are advisory: free text shown to the reviewer in the report. `restrictions` are enforced and can
only **narrow** what the work item and the operator's policy already allow:

| Field | Effect | Rejected when |
|---|---|---|
| `allowed_paths` | replaces the work item's `allowed_paths`; writes elsewhere are blocked by the policy engine and flagged by the verifier (`write_outside_policy`) | a path is not inside the work item's allowed paths, or the list is empty |
| `allowed_commands` | the agent may only run these commands (`run_tests` is unaffected) | a command is not in the operator's `[policy] allowed_commands`, or the list is empty |
| `budget` | lowers any of the budget fields for this job | a value would raise the budget, or the field is unknown |

Every field is optional; omit the whole object for an unrestricted approval. The response echoes what is now
in force as `enforced` and the report carries an "Approval restrictions (enforced)" line.
Errors: 409 when not in webhook mode or when the job was already decided, 403 for self-approval,
422 for invalid input or rejected restrictions (`restrictions rejected: …`); a rejected decision leaves the
job awaiting approval.

### `POST /v1/jobs/{id}/cancel` → 200
`{"by": "alice@corp"}`. Before the job runs it is cancelled immediately; while running, the agent stops at
its next turn, and a command or verifier test run in flight is killed within about a second
(`stop_reason = cancelled`, detail `command interrupted`). A job that was mid-verification finishes as
`cancelled` with the verification flagged `interrupted`; its branch is kept for inspection.

### `POST /v1/jobs/{id}/publish` → 200
`{"actor": "alice@corp", "draft": true, "base": "main", "remote": "origin", "create_pr": true}`. `actor` is
required (every publish is attributed to a person). `remote`, `base` and `draft` default to `[publish]`; the
token and API endpoint always come from `[publish]` and cannot be chosen by the request. Pushes the verified commit and opens a draft PR, exactly
like `gca publish`: the job must be `succeeded` and its branch must still point at the verified commit.
Response: `{"job_id", "branch", "pushed_to", "pr_url", "note"}`; replays return the recorded response without
pushing again. Errors: 403 when `[webhooks] allow_publish = false`, 409 when the job has not succeeded or its
branch moved after verification, 502 when git or GitHub fails. Needs `GITHUB_TOKEN` in gca's environment for
the PR step (push alone works without it).

### `POST /v1/controls/kill-switch` → 200
`{"enabled": true, "reason": "incident 42", "actor": "secops@corp", "scope": "global"}`. `scope` is
`global` (default) or `tenant`, both meaning this gca instance (run one instance per tenant), or `job` with a
`job_id`, which stops that one job and cannot be undone (`enabled` must be `true`). Disengaging the instance
switch requires a non-empty `reason`. Every change is recorded in the hash-chained
`.gca/audit/_controls.jsonl` (job stops in that job's log as `cancel_requested` with `scope = job`).

### `GET /v1/jobs/{id}` → 200
`{"job_id", "status", "title", "error", "cancel_requested", "approver", "branch", "verification_passed", "cost_usd", "updated_at"}`

### `GET /healthz` → 200 (unsigned)
`{"ok": true, "kill_switch": false}`

## Guarantees and limits

- Recording an event never blocks on, or fails because of, the receiver. Events wait in the SQLite outbox
  (`webhook_outbox`) and are retried with exponential backoff (10 s, 20 s, 40 s … capped at 1 h) up to
  `max_attempts`. Non-retryable 4xx responses are marked `dead`; inspect them with `gca webhook-status` and
  replay with `gca webhook-deliver --requeue-dead`.
- Inbound POSTs are idempotent per `X-GCA-Delivery-Id`, including concurrent copies: the id is reserved
  before the command runs, so a retry can never create a second job. A work item submitted without an
  `idempotency_key` gets `delivery:<delivery id>` as its key.
- The kill switch is global to this gca instance (`scope` `global` or `tenant`). Koru's per-tenant switch maps
  onto it, one gca instance per tenant; per-agent (per-job) stops use `scope = "job"`. The tenancy model —
  what is per instance, how identities are trusted, where TLS terminates — is in `docs/multi-tenancy.md`.
- Stops are prompt: a running command or verifier test run is killed within `STOP_POLL_SECONDS` (0.25 s) of
  the cancel or kill switch landing in the queue, in both the host and container workspaces.
- Tested end to end against a mocked platform (`tests/unit/test_webhooks.py`: signing, retries, dead letters,
  approval flow, self-approval, restrictions, idempotent replay, publish, kill switch before and during a run
  and its scopes, real HTTP server; `tests/safety/test_worker_controls.py`: restrictions enforced end to end,
  interruption of a running command and of verification).
  The cross-process contract test (`scripts/webhook_e2e.py`, run in CI) covers submit, replay, allow-list
  refusal, approval with enforced restrictions, the lifecycle, status, publish and both kill-switch scopes
  over real signed HTTP, and regenerates the samples.
  **Not yet exercised against Koru itself.** That needs Koru's endpoint and shared secrets
  (`docs/koru-adapter.md` lists the acceptance run).
