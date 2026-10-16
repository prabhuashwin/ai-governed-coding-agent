"""OpenAI-compatible Chat Completions adapter.

Works with OpenAI, Azure OpenAI (set base_url + auth_header="api-key"), Ollama, vLLM,
LM Studio, OpenRouter, Groq, Together, Mistral, Gemini's OpenAI endpoint, ... anything that
implements POST {base_url}/chat/completions with `tools`.
"""

from __future__ import annotations

import json
import uuid
from typing import Any

import httpx

from ..config import ProviderConfig
from .base import (
    Message,
    ModelResponse,
    ProviderError,
    TextBlock,
    ToolResultBlock,
    ToolUseBlock,
    extract_text_tool_calls,
    parse_tool_arguments,
)
from .http import post_json

DEFAULT_BASE = "https://api.openai.com/v1"


class OpenAICompatibleProvider:
    name = "openai_compatible"

    def __init__(self, cfg: ProviderConfig, transport: httpx.BaseTransport | None = None):
        self.cfg = cfg
        self.model = cfg.model
        self.base_url = (cfg.base_url or DEFAULT_BASE).rstrip("/")
        self._transport = transport
        key = cfg.api_key()
        self.headers = {"content-type": "application/json", **cfg.extra_headers}
        if key:
            if cfg.auth_header.lower() == "authorization":
                self.headers["Authorization"] = f"{cfg.auth_prefix}{key}"
            else:
                self.headers[cfg.auth_header] = key
        elif transport is None and cfg.api_key_env:
            # local servers (Ollama, vLLM) often need no key: only fail if the env var was named explicitly
            if cfg.require_api_key:
                raise ProviderError(f"missing API key: set {cfg.api_key_env}")

    @staticmethod
    def _to_wire(system: str, messages: list[Message]) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = [{"role": "system", "content": system}]
        for m in messages:
            if m.role == "assistant":
                text = "\n".join(b.text for b in m.content if isinstance(b, TextBlock))
                calls = [b for b in m.content if isinstance(b, ToolUseBlock)]
                msg: dict[str, Any] = {"role": "assistant", "content": text or None}
                if calls:
                    msg["tool_calls"] = [
                        {"id": c.id, "type": "function",
                         "function": {"name": c.name, "arguments": json.dumps(c.input)}} for c in calls]
                out.append(msg)
            else:
                for b in m.content:
                    if isinstance(b, ToolResultBlock):
                        content = b.content if not b.is_error else f"ERROR: {b.content}"
                        out.append({"role": "tool", "tool_call_id": b.tool_use_id, "content": content})
                    elif isinstance(b, TextBlock):
                        out.append({"role": "user", "content": b.text})
        return out

    def complete(self, system: str, messages: list[Message], tools: list[dict[str, Any]]) -> ModelResponse:
        body: dict[str, Any] = {
            "model": self.model,
            "messages": self._to_wire(system, messages),
            "tools": [{"type": "function", "function": {"name": t["name"], "description": t["description"],
                                                         "parameters": t["input_schema"]}} for t in tools],
            "max_tokens": self.cfg.max_tokens,
        }
        if self.cfg.temperature is not None:
            body["temperature"] = self.cfg.temperature
        data = post_json(f"{self.base_url}/chat/completions", headers=self.headers, body=body,
                         timeout=self.cfg.timeout_seconds, transport=self._transport)
        try:
            choice = data["choices"][0]
        except (KeyError, IndexError) as e:
            raise ProviderError(f"malformed response: {str(data)[:500]}") from e
        msg = choice.get("message", {})
        content: list[Any] = []
        text = msg.get("content")
        if isinstance(text, list):  # some servers return content parts
            text = "".join(p.get("text", "") for p in text if isinstance(p, dict))
        if text:
            content.append(TextBlock(text=text))
        for tc in msg.get("tool_calls") or []:
            fn = tc.get("function", {})
            content.append(ToolUseBlock(id=tc.get("id") or f"call_{uuid.uuid4().hex[:8]}",
                                        name=fn.get("name", ""), input=parse_tool_arguments(fn.get("arguments"))))
        finish = choice.get("finish_reason") or "stop"
        stop = {"stop": "end_turn", "tool_calls": "tool_use", "length": "max_tokens"}.get(finish, finish)
        if self.cfg.parse_text_tool_calls and text and not any(isinstance(b, ToolUseBlock) for b in content):
            recovered = extract_text_tool_calls(text, {t["name"] for t in tools})
            if recovered:
                content = [TextBlock(text="[tool calls recovered from text]")] + recovered
                stop = "tool_use"
        usage = data.get("usage") or {}
        return ModelResponse(content=content, stop_reason=stop,
                             input_tokens=int(usage.get("prompt_tokens", 0)),
                             output_tokens=int(usage.get("completion_tokens", 0)), raw=data)
