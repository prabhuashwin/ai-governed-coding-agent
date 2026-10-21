# Multi-tenancy: one gca instance per tenant

**Status:** design decision for the Koru pilot (work package 3). Implemented parts are marked; the rest waits
on Koru's identity model.

## Decision

gca is **single-tenant by construction**. A tenant (a Koru organisation, business unit or team that must not
see or affect another's work) gets its own gca instance: its own process, data directory, inbound secret,
outbound secret, repository roots and publishing credentials. The platform runs one instance per tenant and
routes each tenant's commands to its instance.

gca does **not** carry a tenant id on work items, jobs or events, and does not authenticate callers
individually. Everything that would need a tenant id in a shared instance is instead a property of the
instance.

### Why not a shared instance with a tenant column

- The audit log, queue and outbox are files on one host. Isolation inside one process would rest on every
  query filtering by tenant correctly, forever. Separate data directories need no such discipline.
- Repositories are local checkouts. A shared instance would have to map callers to repository roots; a
  per-tenant instance simply has `allowed_repos` set to that tenant's roots.
- The kill switch is the containment control. "Stop everything for tenant A" must not depend on a filter
  being right under pressure. One instance per tenant makes `scope = "tenant"` the instance switch.
- Secrets. The inbound/outbound shared secrets and the GitHub token are per process environment; a shared
  instance would need a secret store keyed by tenant and a token broker. Out of scope for a pilot.
- Blast radius. A bug in one tenant's agent run (runaway command, container escape in worktree mode) cannot
  reach another tenant's checkouts if they are on another host or under another OS user.

The cost is operational: N tenants means N processes to deploy, upgrade and monitor. Section "Operating N
instances" covers that.

## What is per instance (implemented)

| Concern | Setting | Enforced where |
|---|---|---|
| Which repositories may be worked on | `[policy] allowed_repos = ["/srv/repos/tenant-a"]` — a work item's `repo_path` must resolve (symlinks followed) inside one of these roots | `inbound.submit` (403), `gca submit` (exit 2), ServiceNow sync (record error). Empty list = any path (single-developer use only) |
| Who may talk to this instance | `GCA_INBOUND_SECRET` (commands in), `GCA_WEBHOOK_SECRET` (events out) — one pair per instance | signature v2 on every request/event |
| Where results are published | `[publish] remote / base / draft / token_env / api_base` — one GitHub token (or GHE endpoint) per instance, scoped to that tenant's repositories | `gca publish`, `POST /v1/jobs/{id}/publish`. A publish request may narrow `remote`, `base`, `draft`; it cannot name a token or API endpoint |
| Whether the platform may publish at all | `[webhooks] allow_publish` | `POST /v1/jobs/{id}/publish` → 403 |
| Emergency stop | `POST /v1/controls/kill-switch` with `scope` `global` or `tenant` (both = this instance); `agent` + `agent_id` for one platform agent's jobs; `job` for one job | queue `controls` rows (one for the instance, one per stopped agent), claims, worker, running commands, the success transaction |
| Approval deadline | `[approval] timeout_seconds` (ceiling) and per-request `approval_timeout_seconds` | decisions after the deadline refused (409); overdue jobs cancelled by the worker and `webhook-serve` sweeps |
| How people are recorded | `[privacy] opaque_identities`, `identity_pattern` | every identity entry point: webhook, CLI, ServiceNow sync |
| Model and gateway | `[provider]` — a per-tenant gateway route or key | provider construction |
| Budgets and command policy | `[budget]`, `[policy]` | agent loop, verifier |
| Data | `data_dir` (queue, outbox, audit logs, worktrees) | every component |

## Identity: what gca knows about people

gca records **names**, it does not authenticate them. `requester` on a work item, `approver` on a decision
and `actor` on cancel/publish/kill-switch are strings supplied by the caller and are only as trustworthy as
the caller. The caller is the platform, authenticated by the shared secret. So the trust chain is:

```
Koru authenticates the human  →  Koru signs the command with the tenant's secret  →  gca records the name
```

gca enforces the one identity rule it can: a decision's `approver` may not equal the work item's `requester`
(`allow_self_approval = false`). Everything else — who may submit against which repository, who may approve
at which risk level, who may publish — is Koru approval policy, applied before the command reaches gca.

### Opaque identities and "Forget user"

The audit log is append-only and hash-chained: removing or editing an entry breaks `gca audit --verify`, which
is the point of it. A "forget user" request therefore cannot be met by deleting from it. Instead, set

```toml
[privacy]
opaque_identities = true
identity_pattern = "^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$"   # Koru's user-id shape
```

and gca only ever records people as opaque platform user ids. Every identity a caller supplies — `requester`
(required in this mode), `approver`, `actor`, `by`, on the webhook API and the CLI — must match
`identity_pattern`; anything else is refused (422 / exit 2) before anything is written, and the refused value is
not echoed back or stored. The default pattern (letters, digits, `_ . : -`) already excludes email addresses and
"First Last" names; set your platform's exact id shape for a strict guarantee. ServiceNow records are mapped by
`sys_id` (never the display name or user name). Labels gca uses when no person is named (`cli`, `webhook`,
`servicenow`, `gca`, `worker:<id>`) are not people.

Koru keeps the id → person mapping. **Forgetting a user = Koru deletes that mapping.** Every gca record about
the person (audit log, job rows, events already delivered, the PR description) then holds an id that resolves
to no one, and the audit chain stays intact. Nothing in gca needs to be edited.

What opaque identities do **not** cover: free text the caller sends (title, description, acceptance criteria,
approval notes and conditions, cancel and kill-switch reasons) is recorded as given, and the agent's tool calls
record paths and output from the repository. Keep personal data out of task text (Koru can enforce that at its
side); job rows holding it are deleted by `gca prune` after `[retention] jobs_days`, but the audit log keeps the
text. Recording free text as a digest instead is an open item.

**Blocked on Koru:** if Koru's identity model needs gca to distinguish callers *within* a tenant (e.g.
several Koru agents with different repository rights sharing one tenant instance), the smallest change is a
key id: `X-GCA-Key-Id` naming one of several inbound secrets, each with its own `allowed_repos`. The signature
format already binds the body, path and delivery id, so adding a key id is additive. Not built until the need
is confirmed.

## TLS placement

The inbound server is plain HTTP by design: gca does not manage certificates. It binds `127.0.0.1` by default
and **refuses to bind any other address** unless `[webhooks] behind_tls_proxy = true`, which is the operator's
statement that a TLS-terminating proxy (nginx, Caddy, the platform's ingress, an mTLS sidecar) sits in front.
Recommended placements, best first:

1. **Same host, loopback only.** Proxy and gca on one machine; the proxy forwards to `127.0.0.1:8787`. No
   flag needed. Preferred for the pilot.
2. **Private network + proxy.** gca binds the private interface with `behind_tls_proxy = true`; the proxy is
   the only route in. Firewall the port to the proxy's address.
3. **mTLS sidecar** in a container platform: same as 2 with the sidecar as the proxy.

Signatures remain mandatory in all placements: TLS protects confidentiality and integrity on the wire,
the HMAC proves the sender holds the tenant's secret and binds the request to one delivery id.

Outbound events go to `outbound_urls` over whatever scheme is configured; use `https://` URLs. gca verifies
the platform's certificate with the system trust store (httpx defaults).

## Operating N instances

- One OS user and one `data_dir` per tenant; container workspaces (`[workspace] kind = "container"`) for any
  tenant whose repositories are not trusted by the host.
- One `gca.toml` per tenant generated from a template; secrets injected as environment variables from the
  platform's secret store, never written into the file.
- Ports: one inbound port per tenant behind one proxy with path- or host-based routing
  (`https://gca.example/tenant-a/v1/...` → `127.0.0.1:8781`). The signed path is the path gca sees; keep the
  proxy from rewriting it, or sign the rewritten path.
- Upgrades: instances are independent; upgrade one tenant first, watch `job.finished` and
  `gca webhook-status`, then the rest.
- Monitoring: `GET /healthz` per instance (`ok`, `kill_switch`); outbox depth from `gca webhook-status`.

## Not done / open

- Key ids for several callers per instance (above; blocked on Koru identity model).
- Free text in the audit log (task text, notes, reasons) is kept verbatim; a digest mode that records only its
  hash in the log and keeps the text in prunable job rows would let "forget user" cover it too.
- A token broker (short-lived GitHub App installation tokens per tenant instead of a long-lived PAT in the
  environment). `token_env` is the seam: point it at a variable the broker refreshes.
- Automated provisioning of a tenant instance (template + secrets + proxy route). Manual for the pilot;
  see `docs/runbook.md` once written (work package 6).
