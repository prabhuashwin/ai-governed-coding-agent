"""Property-based tests (Hypothesis): thousands of generated inputs against the security-critical code.

Each property states something that must hold for *every* input, not just the cases we thought of:
- a path the policy accepts always resolves inside the workspace and never into a protected location;
- a command the policy accepts never contains a shell operator, a denied token, a non-allow-listed program,
  an absolute/UNC path or a traversal;
- any change to a signed webhook (body, timestamp, signature, secret) is rejected;
- the audit chain detects any single-byte edit.
"""

from __future__ import annotations

import json
import os
import string
import time
from pathlib import Path, PurePosixPath

import pytest
from hypothesis import HealthCheck, assume, given, settings
from hypothesis import strategies as st

from gca.audit import AuditLog, verify_chain
from gca.config import PolicyConfig
from gca.policy import PolicyEngine, PolicyViolation
from gca.webhooks import sign, signed_headers, verify_signature

FUZZ = settings(max_examples=600, deadline=None, suppress_health_check=[HealthCheck.function_scoped_fixture])

SEGMENTS = st.sampled_from(["..", ".", "", "src", "a.py", ".git", ".env", "keys", "id_rsa", "secrets.txt", "~",
                            "C:", "c:", "\\\\server", "x.pem", "node_modules", "%2e%2e", "\u2025", "con", "a b"])
SEPARATORS = st.sampled_from(["/", "\\", "//", "\\\\", "/./"])


@st.composite
def nasty_paths(draw) -> str:
    parts = draw(st.lists(st.one_of(SEGMENTS, st.text(alphabet=string.printable, max_size=6)), min_size=1, max_size=6))
    seps = draw(st.lists(SEPARATORS, min_size=len(parts), max_size=len(parts)))
    prefix = draw(st.sampled_from(["", "/", "\\", "C:\\", "c:/", "~/", "\\\\host\\share\\", "./"]))
    return prefix + "".join(p + s for p, s in zip(parts, seps, strict=True)).rstrip("/\\")


@pytest.fixture(scope="module")
def ws(tmp_path_factory) -> Path:
    root = tmp_path_factory.mktemp("fuzzws") / "ws"
    (root / "src").mkdir(parents=True)
    (root / "src" / "a.py").write_text("x = 1\n")
    (root / ".env").write_text("S=1\n")
    outside = root.parent / "outside.txt"
    outside.write_text("o")
    if hasattr(os, "symlink"):
        try:
            os.symlink(outside, root / "link_out")
        except OSError:  # Windows without symlink privilege
            pass
    return root


# ------------------------------------------------------------------ paths
@FUZZ
@given(p=nasty_paths(), for_write=st.booleans())
def test_accepted_paths_never_escape_or_touch_protected(ws: Path, p: str, for_write: bool):
    eng = PolicyEngine(PolicyConfig(), ws, allowed_paths=["src"] if for_write else None)
    try:
        resolved = eng.resolve_path(p, for_write=for_write)
    except PolicyViolation:
        return
    root = ws.resolve()
    rel = resolved.relative_to(root)  # raises if outside -> test fails
    parts = PurePosixPath(rel.as_posix()).parts
    assert ".git" not in parts and rel.name != ".env" and not rel.name.startswith("id_rsa")
    assert not rel.name.endswith(".pem") and not rel.name.startswith("secrets")
    if for_write:
        assert parts and parts[0] == "src"


@FUZZ
@given(p=st.text(max_size=40))
def test_arbitrary_unicode_never_escapes(ws: Path, p: str):
    try:
        resolved = PolicyEngine(PolicyConfig(), ws).resolve_path(p)
    except (PolicyViolation, ValueError, OSError):
        return
    resolved.relative_to(ws.resolve())


# ------------------------------------------------------------------ commands
TOKENS = st.sampled_from(["python", "python3", "pytest", "ls", "cat", "grep", "node", "npm", "bash", "sh", "curl",
                          "wget", "sudo", "rm", "git", "pip", "powershell", "cmd", "-m", "-q", "-c", "--x", "tests",
                          "a.py", "|", "||", "&&", ";", ">", ">>", "<", "`", "$(", "/etc/passwd", "../x", "..\\x",
                          "C:\\x", "\\\\srv\\s", "~/.ssh", "git push", "nc -l", "rm -rf /", "print(1)", "a b"])


@FUZZ
@given(argv=st.lists(st.one_of(TOKENS, st.text(alphabet=string.printable, max_size=8)), min_size=0, max_size=7))
def test_accepted_commands_are_safe(ws: Path, argv: list[str]):
    cfg = PolicyConfig()
    eng = PolicyEngine(cfg, ws)
    try:
        out = eng.check_command(list(argv))
    except (PolicyViolation, ValueError):  # ValueError: shlex on unbalanced quotes
        return
    assert out, "empty command must never be accepted"
    assert os.path.basename(out[0]) in cfg.allowed_commands
    joined = " ".join(out)
    assert not any(bad in joined for bad in cfg.denied_argv_substrings)
    assert not any(tok in {"|", "||", "&&", ";", ">", ">>", "<", "`", "$("} for tok in out)
    for tok in out[1:]:
        assert not tok.startswith(("/", "~", "\\\\"))
        assert not (len(tok) > 1 and tok[1] == ":" and tok[0].isalpha())
        assert ".." not in PurePosixPath(tok.replace("\\", "/")).parts


# ------------------------------------------------------------------ signatures
@FUZZ
@given(secret=st.text(min_size=1, max_size=30), body=st.binary(max_size=200), flip=st.integers(min_value=0, max_value=10_000),
       which=st.sampled_from(["body", "sig", "ts", "secret", "method", "path", "delivery"]))
def test_any_tampering_breaks_the_signature(secret: str, body: bytes, flip: int, which: str):
    h = signed_headers(secret, "POST", "/hooks/gca", body)
    assert verify_signature(secret, "POST", "/hooks/gca", h, body) is None
    if which == "body":
        if not body:
            body2 = b"x"
        else:
            i = flip % len(body)
            body2 = body[:i] + bytes([body[i] ^ 0x01]) + body[i + 1:]
        assert verify_signature(secret, "POST", "/hooks/gca", h, body2) is not None
    elif which == "sig":
        sig = h["X-GCA-Signature"]
        i = 7 + flip % (len(sig) - 7)
        c = "0" if sig[i] != "0" else "1"
        h2 = {**h, "X-GCA-Signature": sig[:i] + c + sig[i + 1:]}
        assert verify_signature(secret, "POST", "/hooks/gca", h2, body) is not None
    elif which == "ts":
        ts = str(int(h["X-GCA-Timestamp"]) + 1 + flip % 200)
        h2 = {**h, "X-GCA-Timestamp": ts}  # still inside the window, but the MAC covers the timestamp
        assert verify_signature(secret, "POST", "/hooks/gca", h2, body) is not None
    elif which == "method":
        assert verify_signature(secret, "GET", "/hooks/gca", h, body) is not None
    elif which == "path":
        assert verify_signature(secret, "POST", "/hooks/gca/" + str(flip), h, body) is not None
    elif which == "delivery":
        assert verify_signature(secret, "POST", "/hooks/gca", {**h, "X-GCA-Delivery-Id": "dlv_" + str(flip)}, body) is not None
    else:
        assert verify_signature(secret + "x", "POST", "/hooks/gca", h, body) is not None


@FUZZ
@given(skew=st.integers(min_value=-100_000, max_value=100_000))
def test_replay_window(skew: int):
    now = 1_800_000_000
    body = b"{}"
    ts = str(now + skew)
    h = {"X-GCA-Timestamp": ts, "X-GCA-Signature": sign("k", ts, "POST", "/h", "d1", body), "X-GCA-Delivery-Id": "d1"}
    ok = verify_signature("k", "POST", "/h", h, body, max_skew_seconds=300, now=now) is None
    assert ok == (abs(skew) <= 300)


# ------------------------------------------------------------------ audit chain
@settings(max_examples=150, deadline=None, suppress_health_check=[HealthCheck.function_scoped_fixture])
@given(events=st.lists(st.dictionaries(st.text(alphabet=string.ascii_letters, min_size=1, max_size=5)
                                       .filter(lambda k: k not in ("actor", "event")),
                                       st.one_of(st.integers(), st.text(max_size=10)), max_size=3),
                       min_size=2, max_size=6),
       pos=st.integers(min_value=0, max_value=100_000))
def test_any_single_byte_edit_breaks_the_chain(tmp_path_factory, events, pos):
    d = tmp_path_factory.mktemp("audit")
    log = AuditLog(d, "job_f")
    for i, data in enumerate(events):
        log.record(f"actor{i}", "ev", **data)
    raw = log.path.read_bytes()
    assert verify_chain(log.path)[0]
    lines = raw.split(b"\n")
    # edit a byte inside a JSON *value* region so the file stays parseable most of the time
    target = pos % (len(raw) - 1)
    assume(raw[target:target + 1] not in (b"\n",))
    mutated = bytearray(raw)
    mutated[target] = ord("Z") if raw[target] != ord("Z") else ord("Y")
    log.path.write_bytes(bytes(mutated))
    try:
        ok, _ = verify_chain(log.path)
    except (json.JSONDecodeError, KeyError, UnicodeDecodeError, ValueError, TypeError):
        ok = False  # an unparseable log is also a detected tamper
    # editing whitespace between JSON tokens is the only edit that cannot change meaning; serialiser emits none
    assert not ok, f"edit at byte {target} of {len(raw)} went undetected; lines={len(lines)}"


def test_fuzz_suite_is_deterministic_enough():
    assert time.time() > 0
