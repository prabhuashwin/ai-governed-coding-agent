"""Independent verification: never trust the agent's own claim of success.

The verifier works from a *fresh* checkout of the agent's branch, checks that tests were not
deleted or weakened, runs the test command itself, and scans the diff for secret canaries.
"""

from __future__ import annotations

import re
import shutil
import subprocess
import tempfile
from pathlib import Path

from .config import PolicyConfig
from .models import VerificationResult
from .policy import PolicyEngine

TEST_FILE_PATTERNS = [
    r"(^|/)tests?/", r"(^|/)test_[^/]+\.py$", r"_test\.py$", r"\.test\.[cm]?[jt]sx?$", r"\.spec\.[cm]?[jt]sx?$",
    r"(^|/)__tests__/", r"_test\.go$", r"(^|/)spec/",
]
TEST_CONFIG_FILES = {"pytest.ini", "tox.ini", "setup.cfg", "pyproject.toml", "conftest.py", "jest.config.js",
                     "jest.config.ts", "package.json", "vitest.config.ts", ".mocharc.yml"}
PY_TEST_FUNC = re.compile(r"^\s*(?:async\s+)?def\s+test_\w+", re.M)
JS_TEST_FUNC = re.compile(r"^\s*(?:it|test)\s*\(", re.M)
SKIP_MARKERS = re.compile(r"(pytest\.mark\.skip|pytest\.skip\(|@unittest\.skip|\bxit\(|\btest\.skip\(|\bit\.skip\(|\bdescribe\.skip\()")


def is_test_file(path: str) -> bool:
    return any(re.search(p, path) for p in TEST_FILE_PATTERNS)


def _git(repo: Path, *args: str) -> str:
    cp = subprocess.run(["git", *args], cwd=str(repo), capture_output=True, text=True)
    if cp.returncode != 0:
        raise RuntimeError(f"git {' '.join(args)} failed: {cp.stderr.strip()}")
    return cp.stdout


def _show(repo: Path, rev: str, path: str) -> str:
    cp = subprocess.run(["git", "show", f"{rev}:{path}"], cwd=str(repo), capture_output=True, text=True)
    return cp.stdout if cp.returncode == 0 else ""


def _count_tests(path: str, text: str) -> int:
    if path.endswith(".py"):
        return len(PY_TEST_FUNC.findall(text))
    if re.search(r"\.[cm]?[jt]sx?$", path):
        return len(JS_TEST_FUNC.findall(text))
    return 0


class Verifier:
    def __init__(self, repo_path: str | Path, base_commit: str, branch: str, test_command: list[str],
                 policy_cfg: PolicyConfig, *, timeout: int | None = None):
        self.repo = Path(repo_path).resolve()
        self.base = base_commit
        self.branch = branch
        self.test_command = list(test_command)
        self.policy_cfg = policy_cfg
        self.timeout = timeout or policy_cfg.command_timeout_seconds

    def tamper_flags(self) -> list[str]:
        """Detect deleted/weakened tests and test-config changes between base and branch."""
        flags: list[str] = []
        status = _git(self.repo, "diff", "--name-status", f"{self.base}..{self.branch}")
        for line in status.splitlines():
            parts = line.split("\t")
            if len(parts) < 2:
                continue
            code, path = parts[0], parts[-1]
            name = Path(path).name
            if code.startswith("D") and is_test_file(path):
                flags.append(f"test_file_deleted:{path}")
            elif code.startswith("R") and is_test_file(parts[1]) and not is_test_file(path):
                flags.append(f"test_file_renamed_out_of_tests:{parts[1]}->{path}")
            elif code.startswith("M") and is_test_file(path):
                before, after = _show(self.repo, self.base, path), _show(self.repo, self.branch, path)
                nb, na = _count_tests(path, before), _count_tests(path, after)
                if na < nb:
                    flags.append(f"test_count_decreased:{path}:{nb}->{na}")
                sb, sa = len(SKIP_MARKERS.findall(before)), len(SKIP_MARKERS.findall(after))
                if sa > sb:
                    flags.append(f"tests_skipped:{path}:+{sa - sb}")
            if name in TEST_CONFIG_FILES and code.startswith(("M", "D")):
                flags.append(f"test_config_modified:{path}")
        return flags

    def verify(self) -> VerificationResult:
        flags = self.tamper_flags()
        tmp = Path(tempfile.mkdtemp(prefix="gca-verify-"))
        wt = tmp / "wt"
        try:
            _git(self.repo, "worktree", "add", "--detach", str(wt), self.branch)
            policy = PolicyEngine(self.policy_cfg, wt)
            try:
                cp = subprocess.run(self.test_command, cwd=str(wt), env=policy.subprocess_env(),
                                    capture_output=True, text=True, timeout=self.timeout,
                                    stdin=subprocess.DEVNULL, errors="replace")
                exit_code, out, ran = cp.returncode, cp.stdout + ("\n[stderr]\n" + cp.stderr if cp.stderr else ""), True
            except subprocess.TimeoutExpired:
                exit_code, out, ran = -1, f"[test command timed out after {self.timeout}s]", True
            except FileNotFoundError as e:
                exit_code, out, ran = 127, f"test command not found: {e}", False

            diff = _git(self.repo, "diff", "--no-color", f"{self.base}..{self.branch}")
            canaries = policy.find_canaries(diff)
            if canaries:
                flags.append("secret_canary_in_diff")
            numstat = _git(self.repo, "diff", "--numstat", f"{self.base}..{self.branch}")
            ins = dels = 0
            files: list[str] = []
            for line in numstat.splitlines():
                p = line.split("\t")
                if len(p) >= 3:
                    files.append(p[2])
                    if p[0].isdigit() and p[1].isdigit():
                        ins += int(p[0])
                        dels += int(p[1])
            passed = ran and exit_code == 0 and not flags and bool(files)
            return VerificationResult(passed=passed, tests_ran=ran, exit_code=exit_code,
                                      output_tail=out[-4000:], flags=flags, files_changed=files,
                                      insertions=ins, deletions=dels)
        finally:
            subprocess.run(["git", "worktree", "remove", "--force", str(wt)], cwd=str(self.repo), capture_output=True)
            shutil.rmtree(tmp, ignore_errors=True)
            subprocess.run(["git", "worktree", "prune"], cwd=str(self.repo), capture_output=True)
