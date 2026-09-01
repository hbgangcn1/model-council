"""OpenAI-compatible HTTP transport.

Implements the OpenAI Chat Completions API (POST /v1/chat/completions). Works
with:
- DeepSeek (https://api.deepseek.com/v1)
- OpenAI (https://api.openai.com/v1)
- Any OpenAI-compatible provider (LM Studio, vLLM, llama.cpp gateway, etc.)

Configuration via environment:
  OPENAI_COMPAT_BASE: base URL (required, e.g. https://api.deepseek.com/v1)
  OPENAI_COMPAT_KEY: API key (required)
  OPENAI_COMPAT_MODEL_DEFAULT: optional default model

Note: thinking_level is mapped to OpenAI's `reasoning_effort` parameter when the
provider supports it (o1 family, DeepSeek, etc.). For non-reasoning models,
thinking_level is ignored.

Wire format (request):
{
    "model": "...",
    "messages": [{"role": "user", "content": "..."}],
    "max_tokens": ...,
    "temperature": 0,
    "reasoning_effort": "low" | "medium" | "high" | ...  (optional)
}

Response: standard OpenAI ChatCompletion JSON.
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


class OpenAICompatClient(BaseLLMClient):
    """OpenAI-compatible HTTP client."""

    def __init__(
        self,
        base_url: str,
        api_key: Optional[str] = None,
        chat_path: str = "/chat/completions",
    ):
        super().__init__(transport_name="openai_compat")
        self.base_url = base_url.rstrip("/")
        self.chat_path = chat_path.lstrip("/")
        self.api_key = api_key or os.environ.get("OPENAI_COMPAT_KEY", "")

    def _do_call(self, model, thinking_level, prompt, max_tokens, tools=None):
        url = f"{self.base_url}/{self.chat_path}"
        headers = {
            "Content-Type": "application/json",
            "User-Agent": "model-council/15.6",
        }
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"

        payload = {
            "model": model,
            "max_tokens": max_tokens,
            "messages": [{"role": "user", "content": prompt}],
            "temperature": 0,
            "stream": False,
        }
        # Reasoning effort (o1, DeepSeek, etc.)
        if thinking_level and thinking_level != "off":
            payload["reasoning_effort"] = thinking_level

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
                    f"OpenAI-compat HTTP 429: {err_body}",
                    retry_after_s=30.0,
                )
            if 500 <= e.code < 600:
                raise LLMRetryableError(
                    f"OpenAI-compat HTTP {e.code}: {err_body}",
                    retry_after_s=15.0,
                )
            raise LLMPermanentError(
                f"OpenAI-compat HTTP {e.code}: {err_body}", http_status=e.code
            )
        except (TimeoutError, OSError) as e:
            raise LLMRetryableError(
                f"OpenAI-compat network error: {type(e).__name__}: {str(e)[:200]}"
            )

        try:
            data = json.loads(body)
        except json.JSONDecodeError as e:
            raise LLMPermanentError(f"OpenAI-compat returned invalid JSON: {e}")

        if not data.get("choices"):
            raise LLMPermanentError(f"OpenAI-compat returned no choices: {body[:200]}")

        msg = data["choices"][0].get("message") or {}
        text = (msg.get("content") or "").strip()
        if not text and msg.get("reasoning_content"):
            text = f"[thinking]\n{msg['reasoning_content']}"

        if not text:
            raise LLMPermanentError("OpenAI-compat returned empty content")

        meta = CallMeta(
            finish_reason=data["choices"][0].get("finish_reason", "stop"),
            elapsed_s=round(time.time() - t0, 1),
            usage=data.get("usage", {}),
            transport="openai_compat",
        ).as_dict()
        return text, meta