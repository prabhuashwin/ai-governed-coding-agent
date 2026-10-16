"""Anthropic Messages API adapter (plain HTTPS, no SDK dependency)."""

from __future__ import annotations

from typing import Any

import httpx

from ..config import ProviderConfig
from .base import Message, ModelResponse, ProviderError, TextBlock, ToolResultBlock, ToolUseBlock
from .http import post_json

DEFAULT_BASE = "https://api.anthropic.com"


class AnthropicProvider:
    name = "anthropic"

    def __init__(self, cfg: ProviderConfig, transport: httpx.BaseTransport | None = None):
        self.cfg = cfg
        self.model = cfg.model
        self.base_url = (cfg.base_url or DEFAULT_BASE).rstrip("/")
        self._transport = transport
        key = cfg.api_key()
        if not key and transport is None:
            raise ProviderError(f"missing API key: set {cfg.api_key_env}")
        self.headers = {
            "x-api-key": key or "test",
            "anthropic-version": "2023-06-01",
            "content-type": "application/json",
            **cfg.extra_headers,
        }

    @staticmethod
    def _to_wire(messages: list[Message]) -> list[dict[str, Any]]:
        out = []
        for m in messages:
            blocks: list[dict[str, Any]] = []
            for b in m.content:
                if isinstance(b, TextBlock):
                    blocks.append({"type": "text", "text": b.text})
                elif isinstance(b, ToolUseBlock):
                    blocks.append({"type": "tool_use", "id": b.id, "name": b.name, "input": b.input})
                elif isinstance(b, ToolResultBlock):
                    blocks.append({"type": "tool_result", "tool_use_id": b.tool_use_id,
                                   "content": b.content, "is_error": b.is_error})
            out.append({"role": m.role, "content": blocks})
        return out

    def complete(self, system: str, messages: list[Message], tools: list[dict[str, Any]]) -> ModelResponse:
        body: dict[str, Any] = {
            "model": self.model,
            "max_tokens": self.cfg.max_tokens,
            "system": system,
            "messages": self._to_wire(messages),
            "tools": [{"name": t["name"], "description": t["description"], "input_schema": t["input_schema"]} for t in tools],
        }
        if self.cfg.temperature is not None:
            body["temperature"] = self.cfg.temperature
        data = post_json(f"{self.base_url}/v1/messages", headers=self.headers, body=body,
                         timeout=self.cfg.timeout_seconds, transport=self._transport)
        content: list[Any] = []
        for b in data.get("content", []):
            if b.get("type") == "text":
                content.append(TextBlock(text=b.get("text", "")))
            elif b.get("type") == "tool_use":
                content.append(ToolUseBlock(id=b["id"], name=b["name"], input=b.get("input") or {}))
        usage = data.get("usage", {})
        return ModelResponse(
            content=content,
            stop_reason=data.get("stop_reason") or "end_turn",
            input_tokens=int(usage.get("input_tokens", 0)),
            output_tokens=int(usage.get("output_tokens", 0)),
            raw=data,
        )
