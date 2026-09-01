"""StubClient — default fallback when no real transport is configured.

Raises NotImplementedError with a clear message when called. This is the
default final client in `default_client()` so that the system fails loudly
when no LLM transport has been configured (rather than silently hanging).
"""
from __future__ import annotations

from ..llm_client import BaseLLMClient, CallMeta, LLMPermanentError


class StubClient(BaseLLMClient):
    """Placeholder client. Always fails with a clear configuration error."""

    def __init__(self):
        super().__init__(transport_name="stub")

    def _do_call(self, model, thinking_level, prompt, max_tokens, tools=None):
        raise LLMPermanentError(
            "StubClient: no LLM transport configured. Set one of: "
            "DSH_BRIDGE_URL (DSH bridge), "
            "OPENAI_COMPAT_BASE (OpenAI-compatible HTTP), "
            "ANTHROPIC_COMPAT_BASE (Anthropic-compatible HTTP). "
            "See docs/configuration.md."
        )