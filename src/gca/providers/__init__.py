"""Provider registry: build the configured provider. Adding a provider = one class + one line here."""

from __future__ import annotations

from ..config import Config, ProviderConfig
from .base import Message, ModelResponse, Provider, ProviderError, TextBlock, ToolResultBlock, ToolUseBlock


def build_provider(cfg: ProviderConfig) -> Provider:
    kind = cfg.kind.lower()
    if kind == "anthropic":
        from .anthropic import AnthropicProvider
        p: Provider = AnthropicProvider(cfg)
    elif kind in ("openai_compatible", "openai", "azure", "ollama", "vllm", "openrouter", "groq"):
        from .openai_compat import OpenAICompatibleProvider
        p = OpenAICompatibleProvider(cfg)
    elif kind == "scripted":
        from .testing import ScriptedProvider
        if not cfg.script_path:
            raise ProviderError("scripted provider needs provider.script_path")
        p = ScriptedProvider.from_file(cfg.script_path)
    elif kind == "replay":
        from .testing import ReplayProvider
        if not cfg.cassette_path:
            raise ProviderError("replay provider needs provider.cassette_path")
        p = ReplayProvider(cfg.cassette_path)
    else:
        raise ProviderError(f"unknown provider kind: {cfg.kind}")
    if cfg.record and kind not in ("scripted", "replay"):
        from .testing import RecordingProvider
        if not cfg.cassette_path:
            raise ProviderError("record=true needs provider.cassette_path")
        p = RecordingProvider(p, cfg.cassette_path)
    return p


def estimate_cost(config: Config, model: str, input_tokens: int, output_tokens: int) -> float:
    """USD cost from the pricing table (longest matching model prefix wins). Unknown model -> 0."""
    def rate(table: dict[str, float]) -> float:
        best = ""
        for k in table:
            if model.startswith(k) and len(k) > len(best):
                best = k
        return table.get(best, 0.0) if best else 0.0
    return (input_tokens * rate(config.pricing.input_per_million)
            + output_tokens * rate(config.pricing.output_per_million)) / 1_000_000


__all__ = ["Message", "ModelResponse", "Provider", "ProviderError", "TextBlock", "ToolResultBlock",
           "ToolUseBlock", "build_provider", "estimate_cost"]
