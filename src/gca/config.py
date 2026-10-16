"""Configuration: one TOML file selects provider, budgets, policy and pricing.

Provider selection is config-only, so switching from Anthropic to an OpenAI-compatible
server (OpenAI, Azure, Ollama, vLLM, OpenRouter, Groq, ...) never touches code.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field

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
    # scripted / replay providers
    script_path: str | None = None
    cassette_path: str | None = None
    record: bool = False  # wrap a real provider and record a cassette

    def api_key(self) -> str | None:
        return os.environ.get(self.api_key_env) if self.api_key_env else None


class PolicyConfig(BaseModel):
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
    env_passthrough: list[str] = Field(default_factory=lambda: ["PATH", "HOME", "LANG", "LC_ALL", "TMPDIR", "TEMP", "TMP", "SYSTEMROOT", "PYTHONIOENCODING"])
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
    user_env: str = "SN_USER"
    password_env: str = "SN_PASSWORD"
    timeout_seconds: float = 30.0
    poll_seconds: float = 30.0


class Config(BaseModel):
    provider: ProviderConfig = Field(default_factory=ProviderConfig)
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
