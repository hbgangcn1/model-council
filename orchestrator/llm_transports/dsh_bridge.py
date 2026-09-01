"""DSH bridge transport.

Calls `/api/council/llm-stream` exposed by the DeepSeek Harness (DSH)
host-bridge plugin (`~/.dsh/profiles/web/node_modules/dsh-council/index.js`).

DSH internally uses pi-ai to handle wire/auth/retry/protocol differences;
Python just sends a JSON request and reads SSE.

Endpoint contract (matches dsh-council/index.js handleLlmStream):
- POST {url}
  request body: { model, level?, prompt, max_tokens?, system?, temperature? }
  response headers: 200 + text/event-stream
  error responses: 4xx/5xx + JSON { error, code, provider, model }
  SSE events: {event: text|reasoning|usage|finish|error|warning} + [DONE]

This is the default transport for DSH-hosted users (v15.6 backward compat).
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


DEFAULT_DSH_URL = "http://127.0.0.1:3080/api/council/llm-stream"
DEFAULT_IDLE_TIMEOUT_S = 180  # 180s no-byte idle timeout (v15.3 spec)
DEFAULT_TOTAL_TIMEOUT_S = 1800  # 30-minute hard ceiling (v15.3 spec)


def _default_dsh_bridge_url() -> str:
    """Resolve the host-bridge URL from environment and config.

    Priority:
    1. DSH_BRIDGE_URL env var (explicit override)
    2. ~/.dsh/.credentials.yaml DSH_BRIDGE_URL (DSH convention)
    3. DEFAULT_DSH_URL constant
    """
    url = os.environ.get("DSH_BRIDGE_URL", "").strip()
    if url:
        return url
    # Lazy import: only DSH users have config_loader
    try:
        from .. import config_loader  # type: ignore

        creds = config_loader.api_keys()
        url = creds.get("DSH_BRIDGE_URL", "").strip()
        if url:
            return url
    except Exception:
        pass
    return DEFAULT_DSH_URL


class DSHBridgeClient(BaseLLMClient):
    """LLM client that POSTs to the DSH /api/council/llm-stream HTTP endpoint.

    Configuration via environment:
      DSH_BRIDGE_URL: full endpoint URL (default: http://127.0.0.1:3080/api/council/llm-stream)

    The URL resolution chain is:
    - Constructor url argument (explicit)
    - DSH_BRIDGE_URL env var
    - ~/.dsh/.credentials.yaml DSH_BRIDGE_URL field
    - DEFAULT_DSH_URL constant
    """

    def __init__(
        self,
        url: Optional[str] = None,
        idle_timeout_s: float = DEFAULT_IDLE_TIMEOUT_S,
        total_timeout_s: float = DEFAULT_TOTAL_TIMEOUT_S,
    ):
        super().__init__(transport_name="dsh_bridge")
        self.url = url if url else _default_dsh_bridge_url()
        self.idle_timeout_s = idle_timeout_s
        self.total_timeout_s = total_timeout_s

    def _do_call(self, model, thinking_level, prompt, max_tokens, tools=None):
        payload = {
            "model": model,
            "prompt": prompt,
            "max_tokens": max_tokens,
        }
        if thinking_level:
            payload["level"] = thinking_level
        if tools:
            payload["tools"] = tools

        headers = {
            "Content-Type": "application/json",
            "User-Agent": "dsh-council-bridge/1.0",
        }

        events, meta = _stream_sse_lines(
            self.url,
            headers,
            payload,
            idle_timeout=self.idle_timeout_s,
            total_timeout=self.total_timeout_s,
        )

        # Parse SSE events
        text_parts = []
        usage = {}
        finish_reason = "stop"
        dsh_error = None
        for ev in events:
            line = ev.strip()
            if not line or not line.startswith("data:"):
                continue
            data_str = line[len("data:") :].strip()
            if data_str == "[DONE]":
                continue
            try:
                ev_obj = json.loads(data_str)
            except json.JSONDecodeError:
                continue
            event_type = ev_obj.get("event")
            if event_type == "text":
                # DSH bridge streams with {event:"text", delta:"..."}; we accumulate
                # `delta` chunks to reconstruct the full text output.
                text_parts.append(ev_obj.get("delta", ""))
            elif event_type == "reasoning":
                # Reasoning tokens are not user-visible; we don't include them
                pass
            elif event_type == "usage":
                usage = ev_obj.get("usage", usage)
            elif event_type == "finish":
                finish_reason = ev_obj.get("reason", finish_reason)
            elif event_type == "error":
                dsh_error = ev_obj.get("error", "unknown")

        # Handle timeout / network errors from meta
        if meta.get("timeout_kind"):
            raise LLMRetryableError(
                f"DSH bridge timeout ({meta['timeout_kind']}): "
                f"http={meta.get('http_status')}, body={meta.get('http_body')}",
                retry_after_s=5.0,
            )
        if meta.get("network_error"):
            raise LLMRetryableError(
                f"DSH bridge network error: {meta['network_error']}",
                retry_after_s=10.0,
            )
        if dsh_error:
            raise LLMPermanentError(f"DSH bridge provider error: {dsh_error}")

        text = "".join(text_parts).strip()
        if not text:
            raise LLMPermanentError("DSH bridge returned no text content")

        meta_out = CallMeta(
            finish_reason=finish_reason,
            elapsed_s=meta.get("elapsed_s", 0.0),
            usage=usage,
            timeout_kind=meta.get("timeout_kind"),
            http_status=meta.get("http_status"),
            transport="dsh_bridge",
        ).as_dict()
        return text, meta_out


def _stream_sse_lines(url, headers, payload, idle_timeout, total_timeout):
    """Read SSE stream from HTTP POST. Returns (events, meta).

    `events` is a list of stripped lines (each starts with 'data:' for SSE).
    `meta` includes: timeout_kind, elapsed_s, n_events, http_status, http_body, network_error.
    """
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(url, data=data, headers=headers, method="POST")
    events = []
    last_byte_ts = time.time()
    start_ts = time.time()
    timeout_kind = None
    http_status = None
    http_body = None
    network_error = None

    try:
        with urllib.request.urlopen(req, timeout=idle_timeout) as resp:
            for raw in resp:
                line = raw.decode("utf-8", "replace").rstrip("\n")
                if line:
                    events.append(line)
                    last_byte_ts = time.time()
                elif time.time() - last_byte_ts > idle_timeout:
                    timeout_kind = "idle"
                    break
                if time.time() - start_ts > total_timeout:
                    timeout_kind = "total"
                    break
    except urllib.error.HTTPError as e:
        http_status = e.code
        try:
            http_body = e.read(300).decode("utf-8", "replace")[:300]
        except Exception:
            pass
        if 500 <= e.code < 600 or e.code == 429:
            timeout_kind = f"http:{e.code}"
        else:
            timeout_kind = f"http:{e.code}"
    except (TimeoutError, OSError) as e:
        if time.time() - last_byte_ts >= idle_timeout:
            timeout_kind = "idle"
        else:
            timeout_kind = f"network:{type(e).__name__}"
            network_error = str(e)[:200]

    meta = {
        "timeout_kind": timeout_kind,
        "elapsed_s": round(time.time() - start_ts, 1),
        "n_events": len(events),
        "last_byte_age_s": round(time.time() - last_byte_ts, 1),
    }
    if http_status is not None:
        meta["http_status"] = http_status
        meta["http_body"] = http_body
    if network_error:
        meta["network_error"] = network_error
    return events, meta