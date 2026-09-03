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
    LLMQuotaExhaustedError,
    LLMRetryableError,
    LLMPermanentError,
)


DEFAULT_DSH_URL = "http://127.0.0.1:3080/api/council/llm-stream"
DEFAULT_IDLE_TIMEOUT_S = 180  # 180s no-byte idle timeout (v15.3 spec)
DEFAULT_TOTAL_TIMEOUT_S = 1800  # 30-minute hard ceiling (v15.3 spec)


# ===================== 错误分类（限流要分"怎么限的"） =====================
# MiniMax 官方码表（https://platform.minimax.io/docs/api-reference/errorcode）：
# - 1008 insufficient balance / 2056 usage limit exceeded → 额度用尽，不重试
# - 1002 rate limit / 1039 token limit / 1041 conn limit / 2045 rate growth → 可重试
# Z.AI 1302（速率限制）沿用旧结论：可重试。
_QUOTA_CODES = ("1008", "2056")
_RATE_CODES = ("1002", "1039", "1041", "2045", "1302")
_QUOTA_KEYWORDS = (
    "insufficient balance",
    "usage limit exceeded",
    "usage limit",
    "余额不足",
    "额度用尽",
    "额度耗尽",
    "配额耗尽",
    "配额不足",
    "token plan",
    "token_plan",
    "5-hour window",
    "5 hour window",
    "quota_exhausted",
    "quota exhausted",
    "quota exceeded",
)
_RATE_KEYWORDS = (
    "rate limit",
    "rate_limit",
    "too many requests",
    "速率限制",
    "令牌桶",
    "token limit",
    "conn limit",
    "rate growth",
    "server busy",
    "overloaded",
)


def classify_bridge_error(text: str) -> str:
    """把 provider 错误文本分成 quota（额度用尽，不重试）/ rate（可重试）/ unknown。

    纯函数，可离线单测。额度优先：额度文案里常顺带出现 429 字样，
    先判额度再判速率，避免把"额度用尽"误判成"普通限流去重试"。
    """
    low = (text or "").lower()
    if any(c in text for c in _QUOTA_CODES) or any(k in low for k in _QUOTA_KEYWORDS):
        return "quota"
    if (
        "429" in text
        or any(c in text for c in _RATE_CODES)
        or any(k in low for k in _RATE_KEYWORDS)
    ):
        return "rate"
    if any(c in text for c in ("500", "502", "503", "504")) or any(
        k in low for k in ("internal error", "server error", "service unavailable", "timeout", "timed out")
    ):
        return "rate"
    return "unknown"


def _raise_classified(prefix: str, raw: str, http_status=None):
    """按分类抛对应用错：quota→不重试，rate→退避重试，unknown→维持旧行为（永久错）。"""
    kind = classify_bridge_error(raw)
    if kind == "quota":
        raise LLMQuotaExhaustedError(f"QUOTA_EXHAUSTED: {prefix}{raw[:300]}", http_status=http_status)
    if kind == "rate":
        raise LLMRetryableError(f"{prefix}{raw[:300]}", retry_after_s=30.0)
    raise LLMPermanentError(f"{prefix}{raw[:300]}", http_status=http_status)


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
        finish_error = None
        for ev in events:
            line = ev.strip()
            if not line or not line.startswith("data:"):
                continue
            data_str = line[len("data:") :].strip()
            if data_str in ("[DONE]", '"[DONE]"'):
                # 服务端 sseWrite 用 JSON.stringify 包哨兵，线上是带引号的 "[DONE]"；
                # 裸 [DONE] 也兼容（旧桥接曾发裸哨兵）。
                continue
            try:
                ev_obj = json.loads(data_str)
            except json.JSONDecodeError:
                continue
            if not isinstance(ev_obj, dict):
                continue  # 非对象载荷（如纯字符串行）直接跳过，不炸整轮
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
                # provider 业务错误藏在这里：
                # {event:"finish", reason:{kind:"error", failure:{message:"429..."}}}
                # 之前只查 event:"error"，这路错误被静默吞掉（2026-08-28 假成功坑）。
                reason = ev_obj.get("reason", finish_reason)
                if isinstance(reason, dict):
                    if reason.get("kind") == "error":
                        failure = reason.get("failure") or {}
                        finish_error = failure.get("message") or json.dumps(failure)[:300]
                        finish_reason = "error"
                    else:
                        finish_reason = reason.get("kind") or finish_reason
                else:
                    finish_reason = reason
            elif event_type == "error":
                dsh_error = ev_obj.get("error", "unknown")

        # Handle timeout / network errors from meta.
        # HTTP 层 4xx（额度用尽常以 400/402/403 形式出现）不能当超时重试：
        # 先看 body 有没有额度信号，有则直接报额度用尽。
        if meta.get("timeout_kind"):
            tk = meta["timeout_kind"]
            if isinstance(tk, str) and tk.startswith("http:"):
                try:
                    code = int(tk.split(":", 1)[1])
                except ValueError:
                    code = None
                body = str(meta.get("http_body") or "")
                if body and classify_bridge_error(body) == "quota":
                    raise LLMQuotaExhaustedError(
                        f"QUOTA_EXHAUSTED: DSH bridge HTTP {code}: {body[:300]}",
                        http_status=code,
                    )
                if code is not None and 400 <= code < 500 and code != 429:
                    raise LLMPermanentError(
                        f"DSH bridge HTTP {code}: {body[:300]}", http_status=code
                    )
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
        if finish_error:
            _raise_classified("DSH bridge provider error: ", str(finish_error))
        if dsh_error:
            _raise_classified("DSH bridge provider error: ", str(dsh_error))

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