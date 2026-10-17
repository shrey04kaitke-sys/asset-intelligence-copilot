"""Model providers. `get_provider` never substitutes one provider for another."""
from __future__ import annotations

from ..config import Settings
from .base import LLMError, LLMProvider, LLMResponse, ToolCall, ToolResultMsg, Turn, Usage, scrub

__all__ = ["get_provider", "LLMError", "LLMProvider", "LLMResponse", "ToolCall", "ToolResultMsg", "Turn", "Usage", "scrub"]


def get_provider(settings: Settings | None = None) -> LLMProvider:
    settings = settings or Settings()
    if settings.provider == "gemma_api":
        from .gemma_api import GemmaApiProvider
        return GemmaApiProvider(settings)
    if settings.provider == "ollama":
        raise LLMError("unconfigured", "the Ollama provider is not implemented yet; set COPILOT_PROVIDER=gemma_api", provider="ollama")
    raise LLMError("unconfigured", f"unknown COPILOT_PROVIDER '{settings.provider[:30]}' (supported: gemma_api)")
