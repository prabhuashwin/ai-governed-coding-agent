"""Providers for tests and evals: scripted, recording and replay.

- ScriptedProvider: returns a fixed sequence of responses. Tests the loop mechanics
  (retries, budgets, blocked actions, cancellation) with zero network and zero cost.
- RecordingProvider: wraps a real provider and writes every exchange to a cassette.
- ReplayProvider: plays a cassette back, so real-model behaviour is reproducible in CI.
"""

from __future__ import annotations

import hashlib
import json
import time
import uuid
from pathlib import Path
from typing import Any

from .base import Message, ModelResponse, Provider, ProviderError, TextBlock, ToolUseBlock


def _step_to_response(step: dict[str, Any]) -> ModelResponse:
    content: list[Any] = []
    if step.get("text"):
        content.append(TextBlock(text=step["text"]))
    for tc in step.get("tool_calls", []):
        content.append(ToolUseBlock(id=tc.get("id") or f"call_{uuid.uuid4().hex[:8]}",
                                    name=tc["name"], input=tc.get("arguments", {})))
    return ModelResponse(
        content=content,
        stop_reason="tool_use" if step.get("tool_calls") else "end_turn",
        input_tokens=int(step.get("input_tokens", 1000)),
        output_tokens=int(step.get("output_tokens", 100)),
    )


class ScriptedProvider:
    """steps: list of {"text": str, "tool_calls": [{"name": ..., "arguments": {...}}], "input_tokens", "output_tokens"}.
    When the script runs out, `on_exhausted` decides: "finish" (default) emits finish(success=False),
    "repeat_last" repeats the last step forever (useful for budget tests), "error" raises ProviderError."""

    name = "scripted"

    def __init__(self, steps: list[dict[str, Any]], model: str = "scripted-v1", on_exhausted: str = "finish"):
        self.steps = list(steps)
        self.model = model
        self.on_exhausted = on_exhausted
        self.calls: list[dict[str, Any]] = []  # what the loop sent us, for assertions
        self._i = 0

    @classmethod
    def from_file(cls, path: str | Path) -> ScriptedProvider:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
        if isinstance(data, dict):
            return cls(data["steps"], model=data.get("model", "scripted-v1"), on_exhausted=data.get("on_exhausted", "finish"))
        return cls(data)

    def complete(self, system: str, messages: list[Message], tools: list[dict[str, Any]]) -> ModelResponse:
        self.calls.append({"system": system, "messages": [m.model_dump() for m in messages]})
        if self._i < len(self.steps):
            step = self.steps[self._i]
            self._i += 1
            if step.get("sleep"):  # simulate a slow model (used to crash workers mid-run in tests)
                time.sleep(float(step["sleep"]))
            return _step_to_response(step)
        if self.on_exhausted == "error":
            raise ProviderError("scripted provider exhausted")
        if self.on_exhausted == "repeat_last" and self.steps:
            return _step_to_response(self.steps[-1])
        return _step_to_response({"text": "Script exhausted.",
                                  "tool_calls": [{"name": "finish", "arguments": {"summary": "script exhausted", "success": False}}]})


def _request_fingerprint(system: str, messages: list[Message], tools: list[dict[str, Any]]) -> str:
    body = json.dumps({"system": system, "messages": [m.model_dump() for m in messages],
                       "tools": [t["name"] for t in tools]}, sort_keys=True, ensure_ascii=False)
    return hashlib.sha256(body.encode("utf-8")).hexdigest()[:16]


class RecordingProvider:
    name = "recording"

    def __init__(self, inner: Provider, cassette_path: str | Path):
        self.inner = inner
        self.model = inner.model
        self.path = Path(cassette_path)
        self.exchanges: list[dict[str, Any]] = []

    def complete(self, system: str, messages: list[Message], tools: list[dict[str, Any]]) -> ModelResponse:
        resp = self.inner.complete(system, messages, tools)
        self.exchanges.append({"fingerprint": _request_fingerprint(system, messages, tools),
                               "response": resp.model_dump(exclude={"raw"})})
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps({"provider": self.inner.name, "model": self.model,
                                         "exchanges": self.exchanges}, indent=1), encoding="utf-8")
        return resp


class ReplayProvider:
    name = "replay"

    def __init__(self, cassette_path: str | Path, strict: bool = False):
        data = json.loads(Path(cassette_path).read_text(encoding="utf-8"))
        self.model = data.get("model", "replay")
        self.exchanges = data["exchanges"]
        self.strict = strict
        self._i = 0

    def complete(self, system: str, messages: list[Message], tools: list[dict[str, Any]]) -> ModelResponse:
        if self._i >= len(self.exchanges):
            raise ProviderError("cassette exhausted: the run diverged from the recording")
        ex = self.exchanges[self._i]
        self._i += 1
        if self.strict and ex["fingerprint"] != _request_fingerprint(system, messages, tools):
            raise ProviderError(f"cassette mismatch at exchange {self._i}: request differs from recording")
        return ModelResponse.model_validate(ex["response"])
