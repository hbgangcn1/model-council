"""Anthropic-compatible HTTP transport.

Implements the Anthropic Messages API (POST /v1/messages). Works with:
- Anthropic API (https://api.anthropic.com)
- MiniMax anthropic interface (https://api.minimaxi.com/anthropic)
- Any Anthropic-compatible provider (Claude.ai gateway, etc.)

Configuration via environment:
  ANTHROPIC_COMPAT_BASE: base URL (required, e.g. https://api.minimaxi.com/anthropic)
  ANTHROPIC_COMPAT_KEY: API key (required)
  ANTHROPIC_COMPAT_VERSION: API version header (default: 2023-06-01)

Wire format (request):
{
    "model": "...",
    "max_tokens": ...,
    "messages": [{"role": "user", "content": "..."}],
    "temperature": 0,
    "thinking": {"type": "enabled", "budget_tokens": ...}  // optional
}

Response: standard Anthropic Message JSON.
"""
from __future__ import annotations

import json
import os
import time
import urllib.error
import urllib.request
from typing import Optional

from ..llm_client import (
    BaseLLMClient,
    CallMeta,
    LLMRetryableError,
    LLMPermanentError,
)


# Mapping from thinking_level (our internal vocabulary) to anthropic budget_tokens.
# This matches the bridge.py tier-bridge defaults.
_THINKING_BUDGET = {
    "off": 0,
    "minimal": 512,
    "low": 2048,
    "medium": 8192,
    "high": 16384,
}


class AnthropicCompatClient(BaseLLMClient):
    """Anthropic-compatible HTTP client."""

    def __init__(
        self,
        base_url: str,
        api_key: Optional[str] = None,
        messages_path: str = "/v1/messages",
        api_version: str = "2023-06-01",
    ):
        super().__init__(transport_name="anthropic_compat")
        self.base_url = base_url.rstrip("/")
        self.messages_path = messages_path.lstrip("/")
        self.api_key = api_key or os.environ.get("ANTHROPIC_COMPAT_KEY", "")
        self.api_version = api_version or os.environ.get(
            "ANTHROPIC_COMPAT_VERSION", "2023-06-01"
        )

    def _do_call(self, model, thinking_level, prompt, max_tokens, tools=None):
        url = f"{self.base_url}/{self.messages_path}"
        headers = {
            "Content-Type": "application/json",
            "anthropic-version": self.api_version,
            "User-Agent": "model-council/15.6",
        }
        if self.api_key:
            headers["x-api-key"] = self.api_key

        payload = {
            "model": model,
            "max_tokens": max_tokens,
            "messages": [{"role": "user", "content": prompt}],
            "temperature": 0,
        }
        # Anthropic-style thinking budget
        if thinking_level and thinking_level in _THINKING_BUDGET:
            budget = _THINKING_BUDGET[thinking_level]
            if budget > 0:
                payload["thinking"] = {"type": "enabled", "budget_tokens": budget}
            else:
                payload["thinking"] = {"type": "disabled"}

        t0 = time.time()
        try:
            data = json.dumps(payload).encode("utf-8")
            req = urllib.request.Request(url, data=data, headers=headers, method="POST")
            with urllib.request.urlopen(req, timeout=900) as resp:
                body = resp.read().decode("utf-8", "replace")
        except urllib.error.HTTPError as e:
            err_body = ""
            try:
                err_body = e.read(300).decode("utf-8", "replace")[:300]
            except Exception:
                pass
            if e.code == 429:
                raise LLMRetryableError(
                    f"Anthropic-compat HTTP 429: {err_body}",
                    retry_after_s=30.0,
                )
            if 500 <= e.code < 600:
                raise LLMRetryableError(
                    f"Anthropic-compat HTTP {e.code}: {err_body}",
                    retry_after_s=15.0,
                )
            raise LLMPermanentError(
                f"Anthropic-compat HTTP {e.code}: {err_body}", http_status=e.code
            )
        except (TimeoutError, OSError) as e:
            raise LLMRetryableError(
                f"Anthropic-compat network error: {type(e).__name__}: {str(e)[:200]}"
            )

        try:
            data = json.loads(body)
        except json.JSONDecodeError as e:
            raise LLMPermanentError(f"Anthropic-compat returned invalid JSON: {e}")

        parts = []
        for block in data.get("content", []):
            if block.get("type") == "text":
                parts.append(block.get("text", ""))
        text = "\n".join(parts).strip()
        if not text:
            # Fall back to thinking block (if model put everything there)
            for block in data.get("content", []):
                if block.get("type") == "thinking":
                    text = f"[thinking]\n{block.get('thinking', '')}"
                    break

        if not text:
            raise LLMPermanentError("Anthropic-compat returned empty content")

        meta = CallMeta(
            finish_reason=data.get("stop_reason", "stop"),
            elapsed_s=round(time.time() - t0, 1),
            usage=data.get("usage", {}),
            transport="anthropic_compat",
        ).as_dict()
        return text, meta