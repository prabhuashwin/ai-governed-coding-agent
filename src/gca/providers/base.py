"""Provider-agnostic message schema.

The agent loop only speaks this schema. Each adapter translates to/from its API's wire format,
so swapping providers is a config change, and tests can use scripted/replay providers.
"""

from __future__ import annotations

import json
from typing import Any, Literal, Protocol, runtime_checkable

from pydantic import BaseModel, Field


class TextBlock(BaseModel):
    type: Literal["text"] = "text"
    text: str


class ToolUseBlock(BaseModel):
    type: Literal["tool_use"] = "tool_use"
    id: str
    name: str
    input: dict[str, Any] = Field(default_factory=dict)


class ToolResultBlock(BaseModel):
    type: Literal["tool_result"] = "tool_result"
    tool_use_id: str
    content: str
    is_error: bool = False


Block = TextBlock | ToolUseBlock | ToolResultBlock


class Message(BaseModel):
    role: Literal["user", "assistant"]
    content: list[Block]

    @staticmethod
    def user_text(text: str) -> Message:
        return Message(role="user", content=[TextBlock(text=text)])

    def text(self) -> str:
        return "\n".join(b.text for b in self.content if isinstance(b, TextBlock))


class ModelResponse(BaseModel):
    content: list[Block]
    stop_reason: str = "end_turn"  # end_turn | tool_use | max_tokens | error
    input_tokens: int = 0
    output_tokens: int = 0
    raw: dict[str, Any] | None = None

    @property
    def text(self) -> str:
        return "\n".join(b.text for b in self.content if isinstance(b, TextBlock))

    @property
    def tool_calls(self) -> list[ToolUseBlock]:
        return [b for b in self.content if isinstance(b, ToolUseBlock)]

    def as_message(self) -> Message:
        return Message(role="assistant", content=self.content)


class ProviderError(Exception):
    pass


@runtime_checkable
class Provider(Protocol):
    name: str
    model: str

    def complete(self, system: str, messages: list[Message], tools: list[dict[str, Any]]) -> ModelResponse: ...


def extract_text_tool_calls(text: str, known_tools: set[str]) -> list[ToolUseBlock]:
    """Find JSON objects like {"name": "<tool>", "arguments": {...}} in free text (optionally fenced).
    Returns [] unless *every* JSON object found is a valid tool call for a known tool."""
    import re
    import uuid

    body = re.sub(r"```(?:json)?", "", text or "")
    dec = json.JSONDecoder()
    i, calls = 0, []
    while True:
        j = body.find("{", i)
        if j < 0:
            break
        try:
            obj, end = dec.raw_decode(body, j)
        except json.JSONDecodeError:
            i = j + 1
            continue
        i = end
        if not isinstance(obj, dict):
            return []
        name = obj.get("name") or obj.get("tool") or obj.get("function")
        args = obj.get("arguments", obj.get("parameters", obj.get("input", {})))
        if isinstance(name, dict):  # {"function": {"name":..., "arguments":...}}
            args = name.get("arguments", args)
            name = name.get("name")
        if name not in known_tools:
            return []
        calls.append(ToolUseBlock(id=f"text_{uuid.uuid4().hex[:8]}", name=name, input=parse_tool_arguments(args)))
    if not calls:
        return []
    # Accept "some explanation, then the call(s)" — but reject JSON buried mid-prose with more prose after it
    # (that is an explanation quoting a call, not a request). `i` is the end of the last JSON object.
    trailing = re.sub(r"[\s`\[\]]|tool call|\[tool call\]", "", body[i:], flags=re.I)
    if trailing:
        return []
    return calls


def parse_tool_arguments(raw: Any) -> dict[str, Any]:
    """Tool arguments may arrive as a JSON string (OpenAI) or a dict (Anthropic)."""
    if raw is None or raw == "":
        return {}
    if isinstance(raw, dict):
        return raw
    if isinstance(raw, str):
        try:
            v = json.loads(raw)
            return v if isinstance(v, dict) else {"value": v}
        except json.JSONDecodeError:
            return {"_unparsed": raw}
    return {"value": raw}
