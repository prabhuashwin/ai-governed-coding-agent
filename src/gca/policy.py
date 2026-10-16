"""Policy engine: the harness-side controls the model cannot bypass.

Everything here is pure and deterministic so it can be unit-tested exhaustively.
"""

from __future__ import annotations

import fnmatch
import os
import shlex
import time
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath

from .config import PolicyConfig
from .models import Budget, Usage


class PolicyViolation(Exception):
    """Raised when an action is blocked. The message is safe to show to the agent."""


@dataclass
class BudgetTracker:
    budget: Budget
    usage: Usage = field(default_factory=Usage)
    started_at: float = field(default_factory=time.monotonic)

    def charge(self, *, input_tokens: int = 0, output_tokens: int = 0, cost_usd: float = 0.0,
               turns: int = 0, tool_calls: int = 0) -> None:
        self.usage.input_tokens += input_tokens
        self.usage.output_tokens += output_tokens
        self.usage.cost_usd += cost_usd
        self.usage.turns += turns
        self.usage.tool_calls += tool_calls
        self.usage.seconds = time.monotonic() - self.started_at

    def exceeded(self) -> str | None:
        """Return the name of the first exhausted limit, or None."""
        u, b = self.usage, self.budget
        u.seconds = time.monotonic() - self.started_at
        if u.turns >= b.max_turns:
            return "max_turns"
        if u.input_tokens >= b.max_input_tokens:
            return "max_input_tokens"
        if u.output_tokens >= b.max_output_tokens:
            return "max_output_tokens"
        if u.cost_usd >= b.max_cost_usd:
            return "max_cost_usd"
        if u.seconds >= b.max_seconds:
            return "max_seconds"
        if u.tool_calls >= b.max_tool_calls:
            return "max_tool_calls"
        return None

    def remaining_seconds(self) -> float:
        return max(0.0, self.budget.max_seconds - (time.monotonic() - self.started_at))


class PolicyEngine:
    def __init__(self, cfg: PolicyConfig, workspace_root: Path, allowed_paths: list[str] | None = None):
        self.cfg = cfg
        self.root = Path(workspace_root).resolve()
        self.allowed_paths = [p.strip("/") for p in (allowed_paths or []) if p.strip()]

    # ---------------------------------------------------------------- paths
    def resolve_path(self, rel: str, *, for_write: bool = False) -> Path:
        """Confine `rel` to the workspace. Rejects absolute paths, traversal, symlink escapes,
        denied globs and (if configured) paths outside allowed_paths."""
        if not rel or rel.strip() == "":
            raise PolicyViolation("empty path")
        if "\x00" in rel:
            raise PolicyViolation("invalid path")
        p = PurePosixPath(rel.replace("\\", "/"))
        if p.is_absolute() or (len(rel) > 1 and rel[1] == ":"):
            raise PolicyViolation(f"absolute paths are not allowed: {rel}")
        if any(part == ".." for part in p.parts):
            raise PolicyViolation(f"path traversal is not allowed: {rel}")
        candidate = (self.root / Path(*p.parts)).resolve() if p.parts else self.root
        try:
            candidate.relative_to(self.root)
        except ValueError as e:  # symlink pointing outside, etc.
            raise PolicyViolation(f"path escapes workspace: {rel}") from e
        # If the file exists as a symlink chain pointing outside, resolve() already caught it.
        rel_posix = candidate.relative_to(self.root).as_posix()
        for g in self.cfg.denied_path_globs:
            if fnmatch.fnmatch(rel_posix, g) or fnmatch.fnmatch(os.path.basename(rel_posix), g):
                raise PolicyViolation(f"access to protected path is denied: {rel}")
        if for_write and self.allowed_paths:
            if not any(rel_posix == a or rel_posix.startswith(a + "/") for a in self.allowed_paths):
                raise PolicyViolation(f"writes outside allowed paths {self.allowed_paths} are denied: {rel}")
        return candidate

    # ------------------------------------------------------------- commands
    def check_command(self, argv: list[str]) -> list[str]:
        if not argv:
            raise PolicyViolation("empty command")
        if len(argv) == 1 and (" " in argv[0]):
            # agent passed a shell string; tokenise safely rather than invoking a shell
            argv = shlex.split(argv[0])
        joined = " ".join(argv)
        for bad in self.cfg.denied_argv_substrings:
            if bad in joined:
                raise PolicyViolation(f"command contains a denied token ({bad!r})")
        if any(tok in {"|", "||", "&&", ";", ">", ">>", "<", "`", "$("} for tok in argv):
            raise PolicyViolation("shell operators are not allowed; run one program at a time")
        exe = os.path.basename(argv[0])
        if exe not in self.cfg.allowed_commands:
            raise PolicyViolation(
                f"command {exe!r} is not on the allow-list {sorted(self.cfg.allowed_commands)}")
        for tok in argv[1:]:
            if (tok.startswith(("/", "~", "\\")) or (len(tok) > 1 and tok[1] == ":" and tok[0].isalpha())):
                raise PolicyViolation(f"absolute or UNC paths in arguments are not allowed: {tok}")
            if ".." in PurePosixPath(tok.replace("\\", "/")).parts:
                raise PolicyViolation(f"path traversal in arguments is not allowed: {tok}")
        return argv

    def subprocess_env(self) -> dict[str, str]:
        """Minimal environment: nothing secret leaks into agent-run processes."""
        env = {k: v for k, v in os.environ.items() if k in set(self.cfg.env_passthrough)}
        env.setdefault("PYTHONDONTWRITEBYTECODE", "1")
        env.setdefault("PYTHONIOENCODING", "utf-8")
        env["GCA_SANDBOX"] = "1"
        env["NO_COLOR"] = "1"
        env["PIP_NO_INPUT"] = "1"
        return env

    # -------------------------------------------------------------- secrets
    def find_canaries(self, text: str) -> list[str]:
        return [c for c in self.cfg.canary_strings if c and c in text]
