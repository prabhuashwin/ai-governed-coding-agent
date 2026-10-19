# Webhook integration (e.g. Koru)

gca talks to an external agent-operations platform over two signed HTTP channels. Neither side needs the
other's SDK, only this contract.

```
             outbound events (gca → platform)                inbound commands (platform → gca)
 gca ───────────────────────────────────────────►  Koru  ─────────────────────────────────────► gca
   job.submitted, approval.requested, approval.decided,        POST /v1/work-items
   job.started, job.blocked_action, job.verified,              POST /v1/jobs/{id}/decision
   job.finished, job.cancel_requested, control.kill_switch     POST /v1/jobs/{id}/cancel
                                                               POST /v1/controls/kill-switch
                                                               GET  /v1/jobs/{id}
```

## Mapping to Koru capabilities

| Koru capability | Wiring |
|---|---|
| Approval operations (risk, routing, SLA, delegate, conditions) | `approval.mode = "webhook"`. gca emits `approval.requested`; Koru routes it through its approval policy and POSTs the decision back. Conditions are recorded on the approval and in the audit log. Self-approval is refused by gca as well |
| Emergency containment (kill switch) | Koru POSTs `/v1/controls/kill-switch`. Running agents stop at their next turn (`stop_reason = kill_switch`), nothing new is claimed, and disengaging requires a reason |
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
inbound_host = "0.0.0.0"                     # default 127.0.0.1; expose only behind TLS
inbound_port = 8787
events = []                                  # empty = all; e.g. ["approval.requested", "job.finished"]
include_report = true
max_attempts = 8
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
| `approval.decided` | decision (`approve` or `reject`), approver, note, conditions[] |
| `job.started` | worker, attempt, branch, title |
| `job.blocked_action` | tool, args (truncated), reason |
| `job.verified` | passed, flags[], exit_code, files_changed[] |
| `job.finished` | status, error, audit_tail_hash, title, branch, provider, model, requester, approver, source, source_ref, `agent` {stop_reason, claimed_success, blocked_actions, usage}, `verification` {passed, flags, exit_code, files_changed, insertions, deletions}, report_markdown (optional) |
| `job.cancel_requested` | by |
| `control.kill_switch` | enabled, reason, actor (`job_id` is null) |

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
`budget` can only **lower** the operator's configured limits.

### `POST /v1/jobs/{id}/decision` → 200
`{"decision": "approve", "approver": "bob@corp", "note": "…", "conditions": ["touch only src/stats"]}`
Errors: 409 when not in webhook mode or when the job was already decided, 403 for self-approval,
422 for invalid input.

### `POST /v1/jobs/{id}/cancel` → 200
`{"by": "alice@corp"}`. Before the job runs it is cancelled immediately; while running, the agent stops at
its next turn.

### `POST /v1/controls/kill-switch` → 200
`{"enabled": true, "reason": "incident 42", "actor": "secops@corp"}`. Disengaging requires a non-empty
`reason`. Every change is recorded in the hash-chained `.gca/audit/_controls.jsonl`.

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
- The kill switch is global to this gca instance. Koru's per-agent and per-tenant switches map onto it, one gca
  instance per scope.
- Tested end to end against a mocked platform (`tests/unit/test_webhooks.py`: signing, retries, dead letters,
  approval flow, self-approval, idempotent replay, kill switch before and during a run, real HTTP server).
  **Not yet exercised against Koru itself.** That needs Koru's endpoint and shared secrets.
