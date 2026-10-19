"""Tools the agent may call, and the executor that runs them under policy.

The model only ever sees the JSON schema below; every call goes through `ToolExecutor`,
which applies `PolicyEngine` checks *before* touching the workspace.
"""

from __future__ import annotations

import fnmatch
import re
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from .policy import PolicyEngine, PolicyViolation
from .workspace import Workspace

TOOL_SCHEMAS: list[dict[str, Any]] = [
    {
        "name": "list_files",
        "description": "List files under a directory in the repository (relative path, '.' for root). Returns up to 400 entries.",
        "input_schema": {"type": "object", "properties": {"path": {"type": "string", "default": "."},
                                                          "glob": {"type": "string", "description": "optional fnmatch pattern, e.g. '*.py'"}},
                         "required": []},
    },
    {
        "name": "read_file",
        "description": "Read a text file from the repository. Optionally a 1-based line range.",
        "input_schema": {"type": "object", "properties": {"path": {"type": "string"},
                                                          "start_line": {"type": "integer"},
                                                          "end_line": {"type": "integer"}},
                         "required": ["path"]},
    },
    {
        "name": "search",
        "description": "Search file contents with a regular expression. Returns 'path:line: text' matches (max 200).",
        "input_schema": {"type": "object", "properties": {"pattern": {"type": "string"},
                                                          "path": {"type": "string", "default": "."},
                                                          "glob": {"type": "string"}},
                         "required": ["pattern"]},
    },
    {
        "name": "write_file",
        "description": "Create or overwrite a text file with the given content.",
        "input_schema": {"type": "object", "properties": {"path": {"type": "string"}, "content": {"type": "string"}},
                         "required": ["path", "content"]},
    },
    {
        "name": "edit_file",
        "description": "Replace an exact, unique snippet `old` in the file with `new`. Fails if `old` is missing or ambiguous.",
        "input_schema": {"type": "object", "properties": {"path": {"type": "string"}, "old": {"type": "string"},
                                                          "new": {"type": "string"}},
                         "required": ["path", "old", "new"]},
    },
    {
        "name": "run_command",
        "description": "Run one allow-listed program in the repository root (no shell, no pipes). argv is a list, e.g. ['python','-m','pytest','-q'].",
        "input_schema": {"type": "object", "properties": {"argv": {"type": "array", "items": {"type": "string"}}},
                         "required": ["argv"]},
    },
    {
        "name": "run_tests",
        "description": "Run the project's configured test command and return the output.",
        "input_schema": {"type": "object", "properties": {}, "required": []},
    },
    {
        "name": "finish",
        "description": "End the task. Set success=true only if you have run the tests and they pass and every acceptance criterion is met. Summarise what you changed and anything left undone.",
        "input_schema": {"type": "object", "properties": {"summary": {"type": "string"}, "success": {"type": "boolean"}},
                         "required": ["summary", "success"]},
    },
]

TOOL_NAMES = {t["name"] for t in TOOL_SCHEMAS}


def _read_exact(p) -> str:
    """Read text without newline translation, so CRLF files stay CRLF."""
    with open(p, encoding="utf-8", newline="") as f:
        return f.read()


def _write_exact(p, text: str) -> None:
    """Write text without newline translation (no LF->CRLF on Windows)."""
    with open(p, "w", encoding="utf-8", newline="") as f:
        f.write(text)


def _uses_crlf(text: str) -> bool:
    return text.count("\r\n") > 0 and text.count("\r\n") >= text.count("\n") / 2


@dataclass
class ToolResult:
    content: str
    is_error: bool = False
    blocked: bool = False  # policy violation (counts towards max_blocked_actions)
    interrupted: bool = False  # a running command was stopped by a cancel or kill-switch request


class ToolExecutor:
    def __init__(self, workspace: Workspace, policy: PolicyEngine, test_command: list[str],
                 stop_check: Callable[[], bool] | None = None, test_policy: PolicyEngine | None = None):
        self.ws = workspace
        self.policy = policy
        # The work item's test command is part of what the human approved and is what the verifier runs; it is
        # checked against the operator's policy (`test_policy`), not against an approval's narrowed command
        # list, so narrowing `allowed_commands` can never turn `run_tests` into a blocked action.
        self.test_policy = test_policy or policy
        self.test_command = test_command
        self.stop_check = stop_check  # polled while a command runs, so a stop request ends it at once
        self._last_interrupted = False

    def _truncate(self, s: str, limit: int | None = None) -> str:
        limit = limit or self.policy.cfg.max_output_chars
        if len(s) <= limit:
            return s
        head = s[: limit // 2]
        tail = s[-(limit // 2):]
        return f"{head}\n...[truncated {len(s) - limit} chars]...\n{tail}"

    def execute(self, name: str, args: dict[str, Any]) -> ToolResult:
        if name not in TOOL_NAMES:
            return ToolResult(f"unknown tool: {name}", is_error=True)
        self._last_interrupted = False
        try:
            fn = getattr(self, f"_t_{name}")
            out = fn(**args)
            canaries = self.policy.find_canaries(out)
            if canaries:
                return ToolResult("output withheld: it contained protected secret material", is_error=True, blocked=True)
            return ToolResult(out, is_error=self._last_interrupted, interrupted=self._last_interrupted)
        except PolicyViolation as e:
            return ToolResult(f"BLOCKED by policy: {e}", is_error=True, blocked=True)
        except TypeError as e:
            return ToolResult(f"invalid arguments for {name}: {e}", is_error=True)
        except FileNotFoundError as e:
            return ToolResult(f"not found: {e}", is_error=True)
        except UnicodeDecodeError:
            return ToolResult("file is not valid UTF-8 text", is_error=True)
        except Exception as e:  # keep the loop alive; the model can react
            return ToolResult(f"tool error: {type(e).__name__}: {e}", is_error=True)

    # ------------------------------------------------------------------ impl
    def _iter_files(self, rel: str, glob: str | None):
        base = self.policy.resolve_path(rel or ".")
        if base.is_file():
            yield base
            return
        for p in sorted(base.rglob("*")):
            if not p.is_file():
                continue
            rp = p.relative_to(self.policy.root).as_posix()
            if rp == ".git" or rp.startswith(".git/") or "/.git/" in rp or "__pycache__" in rp or "/node_modules/" in rp or rp.startswith("node_modules/"):
                continue
            if glob and not fnmatch.fnmatch(p.name, glob):
                continue
            try:
                self.policy.resolve_path(rp)
            except PolicyViolation:
                continue
            yield p

    def _t_list_files(self, path: str = ".", glob: str | None = None) -> str:
        files = [p.relative_to(self.policy.root).as_posix() for p in self._iter_files(path, glob)]
        extra = "" if len(files) <= 400 else f"\n...and {len(files) - 400} more"
        return "\n".join(files[:400]) + extra if files else "(no files)"

    def _t_read_file(self, path: str, start_line: int | None = None, end_line: int | None = None) -> str:
        p = self.policy.resolve_path(path)
        if p.is_dir():
            raise PolicyViolation(f"{path} is a directory; use list_files")
        text = p.read_text(encoding="utf-8")
        lines = text.splitlines()
        s = max(1, start_line or 1)
        e = min(len(lines), end_line or len(lines))
        body = "\n".join(f"{i:>5}| {lines[i - 1]}" for i in range(s, e + 1))
        return self._truncate(body, self.policy.cfg.max_file_read_chars)

    def _t_search(self, pattern: str, path: str = ".", glob: str | None = None) -> str:
        rx = re.compile(pattern)
        hits: list[str] = []
        for p in self._iter_files(path, glob):
            try:
                for i, line in enumerate(p.read_text(encoding="utf-8").splitlines(), 1):
                    if rx.search(line):
                        hits.append(f"{p.relative_to(self.policy.root).as_posix()}:{i}: {line.strip()[:200]}")
                        if len(hits) >= 200:
                            return "\n".join(hits) + "\n...(capped at 200)"
            except (UnicodeDecodeError, OSError):
                continue
        return "\n".join(hits) if hits else "(no matches)"

    def _t_write_file(self, path: str, content: str) -> str:
        p = self.policy.resolve_path(path, for_write=True)
        p.parent.mkdir(parents=True, exist_ok=True)
        # keep the existing file's line-ending convention when the model sends plain LF content
        if p.is_file() and "\r\n" not in content and _uses_crlf(_read_exact(p)):
            content = content.replace("\n", "\r\n")
        _write_exact(p, content)
        return f"wrote {len(content)} chars to {path}"

    def _t_edit_file(self, path: str, old: str, new: str) -> str:
        p = self.policy.resolve_path(path, for_write=True)
        text = _read_exact(p)
        n = text.count(old)
        if n == 0 and "\r\n" in text and "\r\n" not in old and "\n" in old:
            # the model sees LF; the file on disk is CRLF (common on Windows checkouts)
            old, new = old.replace("\n", "\r\n"), new.replace("\r\n", "\n").replace("\n", "\r\n")
            n = text.count(old)
        if n == 0:
            return "edit failed: `old` not found in file (read the file and copy the snippet exactly)"
        if n > 1:
            return f"edit failed: `old` occurs {n} times; include more context to make it unique"
        if "\r\n" in text and "\r\n" not in new and "\n" in new and _uses_crlf(text):
            new = new.replace("\n", "\r\n")
        _write_exact(p, text.replace(old, new, 1))
        return f"edited {path}"

    def _run(self, argv: list[str], policy: PolicyEngine | None = None) -> str:
        argv = (policy or self.policy).check_command(list(argv))
        res = self.ws.run(argv, env=self.policy.subprocess_env(), timeout=self.policy.cfg.command_timeout_seconds,
                          stop_check=self.stop_check)
        self._last_interrupted = res.interrupted
        return self._truncate(f"$ {' '.join(argv)}\n[exit {res.exit_code}]\n{res.output}")

    def _t_run_command(self, argv: list[str]) -> str:
        if isinstance(argv, str):
            argv = [argv]
        return self._run(argv)

    def _t_run_tests(self) -> str:
        return self._run(list(self.test_command), policy=self.test_policy)

    def _t_finish(self, summary: str, success: bool) -> str:
        return "finished"
