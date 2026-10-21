"""Configuration: one TOML file selects provider, budgets, policy and pricing.

Provider selection is config-only, so switching from Anthropic to an OpenAI-compatible
server (OpenAI, Azure, Ollama, vLLM, OpenRouter, Groq, ...) never touches code.
"""

from __future__ import annotations

import os
import re
import sys
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field, field_validator

from .models import Budget

if sys.version_info >= (3, 11):
    import tomllib
else:  # pragma: no cover - exercised on 3.10 hosts
    import tomli as tomllib  # type: ignore[no-redef]


class ProviderConfig(BaseModel):
    kind: str = "anthropic"  # anthropic | openai_compatible | scripted | replay
    model: str = "claude-sonnet-4-5"
    base_url: str | None = None
    api_key_env: str = "ANTHROPIC_API_KEY"
    require_api_key: bool = True  # set false for local servers (Ollama, vLLM)
    auth_header: str = "Authorization"  # openai_compatible: "api-key" for Azure OpenAI
    auth_prefix: str = "Bearer "
    extra_headers: dict[str, str] = Field(default_factory=dict)
    timeout_seconds: float = 120.0
    max_tokens: int = 4096
    temperature: float | None = None
    # openai_compatible: if a reply has no structured tool_calls but its text is JSON tool-call objects
    # (common with small local models), parse them as tool calls. Logged in the audit trail.
    parse_text_tool_calls: bool = True
    # scripted / replay providers
    script_path: str | None = None
    cassette_path: str | None = None
    record: bool = False  # wrap a real provider and record a cassette

    def api_key(self) -> str | None:
        return os.environ.get(self.api_key_env) if self.api_key_env else None


class PolicyConfig(BaseModel):
    # Repositories this instance may work on: directories whose subtree may be a work item's repo_path.
    # Empty = any path on this host (single-developer use). Set it for every shared or platform-driven deployment:
    # a signed submission can otherwise point the agent at any checkout the gca user can read.
    allowed_repos: list[str] = Field(default_factory=list)
    # argv[0] values the agent may run via run_command. Everything else is blocked.
    allowed_commands: list[str] = Field(
        default_factory=lambda: ["python", "python3", "pytest", "npm", "node", "npx", "ls", "cat", "grep"]
    )
    # Substrings that are never allowed anywhere in an argv (defence in depth).
    denied_argv_substrings: list[str] = Field(
        default_factory=lambda: ["curl", "wget", "ssh", "scp", "nc ", "sudo", "rm -rf /", "git push"]
    )
    # Files the agent must not read or write even inside the workspace.
    denied_path_globs: list[str] = Field(
        default_factory=lambda: [".git", ".git/**", ".env", ".env.*", "**/*.pem", "**/id_rsa*", "**/secrets*"]
    )
    # Environment variables passed through to subprocesses. Everything else is stripped.
    env_passthrough: list[str] = Field(default_factory=lambda: ["PATH", "HOME", "LANG", "LC_ALL", "TMPDIR", "TEMP", "TMP", "SYSTEMROOT", "PYTHONIOENCODING",
                                                                       "PATHEXT", "COMSPEC", "WINDIR"])
    command_timeout_seconds: int = 300
    max_output_chars: int = 12_000
    max_file_read_chars: int = 60_000
    # After this many blocked actions in one run the agent is halted (likely injection or loop).
    max_blocked_actions: int = 5
    # Any blocked action fails the job (it still produces a branch + report for human review).
    fail_on_any_blocked_action: bool = True
    # Secret canaries: if any of these strings appears in agent output/diff the run fails.
    canary_strings: list[str] = Field(default_factory=list)


class PricingConfig(BaseModel):
    """USD per 1M tokens, keyed by model name (prefix match). Unknown models cost 0 with a warning."""

    input_per_million: dict[str, float] = Field(default_factory=dict)
    output_per_million: dict[str, float] = Field(default_factory=dict)


class WorkspaceConfig(BaseModel):
    kind: str = "worktree"  # worktree | container
    runtime: str = "docker"  # docker | podman (container kind only)
    image: str = "python:3.12-slim"
    network: str = "none"
    memory: str = "2g"
    cpus: str = "2"
    pids_limit: int = 256
    extra_args: list[str] = Field(default_factory=list)
    run_as_host_user: bool = True


class ServiceNowConfig(BaseModel):
    instance_url: str = ""  # e.g. https://dev12345.service-now.com
    table: str = "x_gca_work_item"
    # "" for a scoped-app table (fields like repo_path); "u_" for a global custom table (u_repo_path)
    field_prefix: str = ""
    user_env: str = "SN_USER"
    password_env: str = "SN_PASSWORD"
    timeout_seconds: float = 30.0
    poll_seconds: float = 30.0


class WebhookConfig(BaseModel):
    """Outbound events (gca -> your platform) and inbound commands (your platform -> gca), HMAC-signed."""

    enabled: bool = False
    outbound_urls: list[str] = Field(default_factory=list)
    outbound_secret_env: str = "GCA_WEBHOOK_SECRET"
    events: list[str] = Field(default_factory=list)  # empty = all event types
    include_report: bool = True  # include the markdown report in job.finished
    deliver_inline: bool = True  # try to deliver as soon as an event is recorded (else only on flush)
    timeout_seconds: float = 5.0
    max_attempts: int = 8
    # inbound command server (`gca webhook-serve`)
    inbound_host: str = "127.0.0.1"
    inbound_port: int = 8787
    inbound_secret_env: str = "GCA_INBOUND_SECRET"
    allow_publish: bool = True  # accept POST /v1/jobs/{id}/publish (push the verified commit, open a draft PR)
    behind_tls_proxy: bool = False  # required to bind a non-loopback inbound_host: the server itself is plain HTTP
    max_skew_seconds: int = 300  # reject signed requests older/newer than this
    source_name: str = "gca"  # appears as `source` in every event envelope


class PublishConfig(BaseModel):
    """Where a job's branch goes when it is published. One instance = one tenant = one set of credentials."""
    remote: str = "origin"
    base: str | None = None  # default: the remote's HEAD
    draft: bool = True
    token_env: str = "GITHUB_TOKEN"  # environment variable holding this tenant's GitHub token (GH_TOKEN also read)
    api_base: str = "https://api.github.com"  # GitHub Enterprise: https://ghe.example/api/v3


class RetentionConfig(BaseModel):
    """How long `gca prune` keeps rows in the job store. Audit logs are never pruned by gca."""
    jobs_days: int = 90      # terminal jobs (work item, result, report) after their last update
    outbox_days: int = 14    # delivered and dead outbound events
    inbound_days: int = 14   # inbound idempotency records; must exceed the platform's longest retry horizon


MAX_APPROVAL_TIMEOUT_SECONDS = 90 * 24 * 3600  # 90 days

# Default shape of an opaque user id: a token such as a UUID or "u_12ab…". No "@" and no spaces, so neither an
# email address nor a "First Last" name can match. Set your platform's exact id shape for a strict guarantee.
DEFAULT_IDENTITY_PATTERN = r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}$"


class PrivacyConfig(BaseModel):
    """How people are identified in what gca stores. The audit log is append-only and hash-chained, so it cannot
    forget anyone; with `opaque_identities` it never learns who anyone is. Every identity a caller supplies
    (requester, approver, actor, by) must then be an opaque platform user id matching `identity_pattern`; the
    platform keeps the id -> person mapping, and deleting that mapping is what "forget user" means for gca."""
    opaque_identities: bool = False
    identity_pattern: str = DEFAULT_IDENTITY_PATTERN

    @field_validator("identity_pattern")
    @classmethod
    def _compiles(cls, v: str) -> str:
        try:
            re.compile(v)
        except re.error as e:
            raise ValueError(f"identity_pattern is not a valid regular expression: {e}") from e
        return v


class ApprovalConfig(BaseModel):
    # local: `gca approve` (or ServiceNow mirror). webhook: approval comes only from an inbound signed decision.
    mode: str = "local"
    allow_self_approval: bool = False  # enforced for webhook decisions
    # Longest a job may wait for a decision; None = no limit. A submission may ask for a shorter window
    # (`approval_timeout_seconds`), never a longer one. Overdue jobs end `cancelled` with error "approval_expired".
    timeout_seconds: int | None = Field(default=None, gt=0, le=MAX_APPROVAL_TIMEOUT_SECONDS)


class Config(BaseModel):
    provider: ProviderConfig = Field(default_factory=ProviderConfig)
    webhooks: WebhookConfig = Field(default_factory=WebhookConfig)
    approval: ApprovalConfig = Field(default_factory=ApprovalConfig)
    publish: PublishConfig = Field(default_factory=PublishConfig)
    retention: RetentionConfig = Field(default_factory=RetentionConfig)
    privacy: PrivacyConfig = Field(default_factory=PrivacyConfig)
    workspace: WorkspaceConfig = Field(default_factory=WorkspaceConfig)
    servicenow: ServiceNowConfig = Field(default_factory=ServiceNowConfig)
    budget: Budget = Field(default_factory=Budget)
    policy: PolicyConfig = Field(default_factory=PolicyConfig)
    pricing: PricingConfig = Field(default_factory=PricingConfig)
    data_dir: str = ".gca"
    worker_id: str = "worker-1"
    stale_heartbeat_seconds: int = 600

    @property
    def db_path(self) -> Path:
        return Path(self.data_dir) / "jobs.sqlite3"

    @property
    def audit_dir(self) -> Path:
        return Path(self.data_dir) / "audit"


def load_config(path: str | Path | None = None) -> Config:
    """Load config from TOML; missing file -> defaults. GCA_CONFIG env var overrides path."""
    p = Path(path or os.environ.get("GCA_CONFIG", "gca.toml"))
    data: dict[str, Any] = {}
    if p.exists():
        with p.open("rb") as f:
            data = tomllib.load(f)
    return Config.model_validate(data)
