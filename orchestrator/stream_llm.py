"""Stream LLM (v15.6+).

Thin compatibility wrapper that delegates to `orchestrator.llm_client`. The
actual transport selection (DSH bridge, OpenAI-compat, Anthropic-compat, stub)
happens in `llm_client.default_client()`.

Public API (unchanged since v15.5):
- `call_stream(model, thinking_level, prompt, max_tokens)` → `(text, meta)`
- `bridge_stream(...)`: backward-compat alias for `call_stream` (v15.6 L3
  bridge terminology; some callers still use this name)

Meta fields (returned to callers; matches what council_v14.py expects):
- `finish_reason`: "stop" | "length" | "error"
- `elapsed_s`: float, total wall time
- `usage`: dict with `promptTokens` / `completionTokens` / `cacheHitTokens` /
  `totalTokens` (matches v15.3+ cost_calibrate accounting)
- `timeout_kind`: None | "idle" | "total" | "retries_exhausted" | "http:XXX"
- `http_status`: int | None
- `retry_count`: int
- `transport`: name of the transport that handled the request
"""
from __future__ import annotations

import threading
from typing import Optional

try:
    from . import llm_client
except ImportError:
    import llm_client  # type: ignore

# Module-level singleton; lazily initialized; thread-safe.
_client = None
_client_lock = threading.Lock()


def _get_client():
    """Return the module-level CompositeClient, lazily constructing it."""
    global _client
    if _client is None:
        with _client_lock:
            if _client is None:
                _client = llm_client.default_client()
    return _client


def call_stream(
    model: str,
    thinking_level: str,
    prompt: str,
    max_tokens: int,
    tools: Optional[list] = None,
):
    """Call LLM via the configured transport. Returns (text, meta).

    Args:
      model: provider/model identifier (e.g. "deepseek-v4-pro")
      thinking_level: one of "off" | "minimal" | "low" | "medium" | "high" | "max"
      prompt: user prompt (str)
      max_tokens: max output tokens
      tools: optional list of tool schemas (function-calling); only DSH/OpenAI/Anthropic
             transports that support tools will use this.

    Returns:
      (text, meta) — text is the model output (str), meta is a dict.
    """
    return _get_client().call_stream(model, thinking_level, prompt, max_tokens, tools)


def bridge_stream(model: str, thinking_level: str, prompt: str, max_tokens: int):
    """Backward-compat alias for `call_stream`.

    Some callers (e.g. legacy benchmark code) still use the v15.6 L3 name
    `bridge_stream`. This alias preserves source compatibility.
    """
    return call_stream(model, thinking_level, prompt, max_tokens)


# ===================== v15.5 backwards-compat surface =====================
# Some legacy code paths may still import these constants. Keep them defined
# but route through the new client.
DEFAULT_DSH_URL = "http://127.0.0.1:3080/api/council/llm-stream"
IDLE_TIMEOUT_S = 180
TOTAL_TIMEOUT_S = 1800