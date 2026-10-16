"""Provider adapters, exercised against mocked HTTP transports (no network, no keys)."""

from __future__ import annotations

import json
from pathlib import Path

import httpx
import pytest

from gca.config import Config, ProviderConfig
from gca.providers import build_provider, estimate_cost
from gca.providers.anthropic import AnthropicProvider
from gca.providers.base import Message, ProviderError, TextBlock, ToolResultBlock, ToolUseBlock
from gca.providers.openai_compat import OpenAICompatibleProvider
from gca.providers.testing import RecordingProvider, ReplayProvider, ScriptedProvider
from gca.tools import TOOL_SCHEMAS

CONVO = [
    Message.user_text("fix it"),
    Message(role="assistant", content=[TextBlock(text="reading"), ToolUseBlock(id="t1", name="read_file", input={"path": "a.py"})]),
    Message(role="user", content=[ToolResultBlock(tool_use_id="t1", content="x = 1", is_error=False)]),
]


def test_anthropic_wire_format_and_parsing():
    seen = {}

    def handler(req: httpx.Request) -> httpx.Response:
        seen["headers"] = dict(req.headers)
        seen["body"] = json.loads(req.content)
        return httpx.Response(200, json={
            "content": [{"type": "text", "text": "ok"},
                        {"type": "tool_use", "id": "tu_1", "name": "edit_file", "input": {"path": "a.py", "old": "1", "new": "2"}}],
            "stop_reason": "tool_use", "usage": {"input_tokens": 120, "output_tokens": 30}})

    p = AnthropicProvider(ProviderConfig(kind="anthropic", model="claude-x", api_key_env="NOPE_KEY"),
                          transport=httpx.MockTransport(handler))
    resp = p.complete("SYS", CONVO, TOOL_SCHEMAS)
    body = seen["body"]
    assert body["system"] == "SYS" and body["model"] == "claude-x"
    assert body["messages"][1]["content"][1] == {"type": "tool_use", "id": "t1", "name": "read_file", "input": {"path": "a.py"}}
    assert body["messages"][2]["content"][0]["type"] == "tool_result"
    assert body["tools"][0]["name"] == "list_files" and "input_schema" in body["tools"][0]
    assert seen["headers"]["anthropic-version"] == "2023-06-01"
    assert resp.text == "ok" and resp.tool_calls[0].name == "edit_file" and resp.tool_calls[0].input["new"] == "2"
    assert resp.input_tokens == 120 and resp.stop_reason == "tool_use"


def test_openai_compatible_wire_format_and_parsing():
    seen = {}

    def handler(req: httpx.Request) -> httpx.Response:
        seen["headers"] = dict(req.headers)
        seen["body"] = json.loads(req.content)
        seen["url"] = str(req.url)
        return httpx.Response(200, json={
            "choices": [{"finish_reason": "tool_calls", "message": {
                "content": None,
                "tool_calls": [{"id": "call_9", "type": "function",
                                "function": {"name": "run_tests", "arguments": "{}"}},
                               {"id": "call_10", "type": "function",
                                "function": {"name": "edit_file", "arguments": "{\"path\": \"a.py\", \"old\": \"1\", \"new\": \"2\"}"}}]}}],
            "usage": {"prompt_tokens": 200, "completion_tokens": 40}})

    cfg = ProviderConfig(kind="openai_compatible", model="gpt-x", base_url="http://localhost:11434/v1",
                         api_key_env="NOPE_KEY", require_api_key=False)
    p = OpenAICompatibleProvider(cfg, transport=httpx.MockTransport(handler))
    resp = p.complete("SYS", CONVO, TOOL_SCHEMAS)
    b = seen["body"]
    assert seen["url"].endswith("/v1/chat/completions")
    assert b["messages"][0] == {"role": "system", "content": "SYS"}
    assert b["messages"][2]["role"] == "assistant" and b["messages"][2]["tool_calls"][0]["function"]["name"] == "read_file"
    assert b["messages"][3] == {"role": "tool", "tool_call_id": "t1", "content": "x = 1"}
    assert b["tools"][0]["type"] == "function" and "parameters" in b["tools"][0]["function"]
    assert [c.name for c in resp.tool_calls] == ["run_tests", "edit_file"]
    assert resp.tool_calls[1].input == {"path": "a.py", "old": "1", "new": "2"}
    assert resp.stop_reason == "tool_use" and resp.input_tokens == 200


def test_openai_auth_header_variants(monkeypatch):
    monkeypatch.setenv("K", "sekrit")
    seen = {}

    def handler(req: httpx.Request) -> httpx.Response:
        seen.update(dict(req.headers))
        return httpx.Response(200, json={"choices": [{"message": {"content": "hi"}, "finish_reason": "stop"}]})

    p = OpenAICompatibleProvider(ProviderConfig(kind="openai_compatible", api_key_env="K"), transport=httpx.MockTransport(handler))
    p.complete("s", [Message.user_text("x")], [])
    assert seen["authorization"] == "Bearer sekrit"
    p2 = OpenAICompatibleProvider(ProviderConfig(kind="openai_compatible", api_key_env="K", auth_header="api-key"),
                                  transport=httpx.MockTransport(handler))
    p2.complete("s", [Message.user_text("x")], [])
    assert seen["api-key"] == "sekrit"


def test_retry_then_error(monkeypatch):
    calls = {"n": 0}

    def handler(req: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        return httpx.Response(500, text="boom")

    monkeypatch.setattr("gca.providers.http.time.sleep", lambda s: None)
    p = AnthropicProvider(ProviderConfig(api_key_env="NOPE"), transport=httpx.MockTransport(handler))
    with pytest.raises(ProviderError):
        p.complete("s", [Message.user_text("x")], [])
    assert calls["n"] == 4  # retried


def test_missing_api_key_is_a_clear_error(monkeypatch):
    monkeypatch.delenv("DEFINITELY_UNSET_KEY", raising=False)
    with pytest.raises(ProviderError, match="DEFINITELY_UNSET_KEY"):
        build_provider(ProviderConfig(kind="anthropic", api_key_env="DEFINITELY_UNSET_KEY"))
    with pytest.raises(ProviderError, match="DEFINITELY_UNSET_KEY"):
        build_provider(ProviderConfig(kind="openai_compatible", api_key_env="DEFINITELY_UNSET_KEY"))
    # local servers may opt out
    assert build_provider(ProviderConfig(kind="openai_compatible", api_key_env="", require_api_key=False)).name == "openai_compatible"


def test_scripted_record_replay(tmp_path: Path):
    scripted = ScriptedProvider([{"text": "a", "tool_calls": [{"name": "run_tests", "arguments": {}}]}, {"text": "b"}])
    rec = RecordingProvider(scripted, tmp_path / "c.json")
    r1 = rec.complete("s", [Message.user_text("x")], TOOL_SCHEMAS)
    r2 = rec.complete("s", [Message.user_text("x"), r1.as_message()], TOOL_SCHEMAS)
    assert r1.tool_calls[0].name == "run_tests" and r2.text == "b"
    rep = ReplayProvider(tmp_path / "c.json", strict=True)
    assert rep.complete("s", [Message.user_text("x")], TOOL_SCHEMAS).tool_calls[0].name == "run_tests"
    with pytest.raises(ProviderError, match="mismatch"):
        rep.complete("s", [Message.user_text("DIFFERENT")], TOOL_SCHEMAS)
    rep2 = ReplayProvider(tmp_path / "c.json")
    rep2.complete("s", [], [])
    rep2.complete("s", [], [])
    with pytest.raises(ProviderError, match="exhausted"):
        rep2.complete("s", [], [])


def test_scripted_exhaustion_modes():
    p = ScriptedProvider([{"text": "x"}], on_exhausted="finish")
    p.complete("s", [], [])
    r = p.complete("s", [], [])
    assert r.tool_calls[0].name == "finish" and r.tool_calls[0].input["success"] is False
    p2 = ScriptedProvider([{"text": "x"}], on_exhausted="error")
    p2.complete("s", [], [])
    with pytest.raises(ProviderError):
        p2.complete("s", [], [])


def test_pricing_prefix_match_and_unknown_model():
    cfg = Config()
    cfg.pricing.input_per_million = {"claude": 1.0, "claude-sonnet": 3.0}
    cfg.pricing.output_per_million = {"claude": 5.0, "claude-sonnet": 15.0}
    assert estimate_cost(cfg, "claude-sonnet-4-5", 1_000_000, 0) == 3.0  # longest prefix wins
    assert estimate_cost(cfg, "claude-haiku", 0, 1_000_000) == 5.0
    assert estimate_cost(cfg, "gpt-x", 1_000_000, 1_000_000) == 0.0


def test_config_toml_round_trip(tmp_path: Path, monkeypatch):
    p = tmp_path / "gca.toml"
    p.write_text('data_dir = "d"\n[provider]\nkind = "openai_compatible"\nmodel = "m"\nbase_url = "http://x/v1"\n'
                 '[budget]\nmax_turns = 3\n[policy]\nallowed_commands = ["python"]\n')
    from gca.config import load_config
    cfg = load_config(p)
    assert cfg.provider.kind == "openai_compatible" and cfg.budget.max_turns == 3 and cfg.policy.allowed_commands == ["python"]
    assert cfg.db_path == Path("d") / "jobs.sqlite3"
    monkeypatch.setenv("GCA_CONFIG", str(p))
    assert load_config().provider.model == "m"
    assert load_config(tmp_path / "missing.toml").provider.kind == "anthropic"  # defaults
