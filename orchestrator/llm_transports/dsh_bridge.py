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
import uuid
from typing import Optional

from ..llm_client import (
    BaseLLMClient,
    CallMeta,
    LLMQuotaExhaustedError,
    LLMRetryableError,
    LLMPermanentError,
)


def bridge_session_id() -> str:
    """桥接会话 ID（v15.9，根因见 DESIGN-v14.md v15.9）：
    OpenCode Zen 要求每个会话带稳定的 session 标识（MissingSessionID 否则
    免费档 400 拒绝）。DSH 主会话经 agent-loop 带 DSH session id 所以正常；
    council 经桥调用从不带 id，muse 全挂。进程级稳定 = 单 run 稳定
    （插件每 run 起一个 python 进程，同 run 内共享 id 还利于 prompt 缓存命中）。
    环境变量 COUNCIL_SESSION_ID 可覆盖（调试/连续性需要时）。"""
    env = os.environ.get("COUNCIL_SESSION_ID", "").strip()
    if env:
        return env
    return f"dsh-council-{uuid.uuid4().hex[:12]}"


_BRIDGE_SESSION_ID = bridge_session_id()


DEFAULT_DSH_URL = "http://127.0.0.1:3080/api/council/llm-stream"
DEFAULT_IDLE_TIMEOUT_S = 180  # 180s no-byte idle timeout (v15.3 spec)
DEFAULT_TOTAL_TIMEOUT_S = 1800  # 30-minute hard ceiling (v15.3 spec)
DEFAULT_TOOL_EXEC_TIMEOUT_S = 90  # tool-exec 单次执行上限（搜索/抓取）


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
# 空回复：模型正常结束但零内容块（pi-ai EMPTY_RESPONSE，如静默拒答；偶发退化空包）。
# 与 quota/rate 文本无交集，独立分类、可短退避重试（与 pi-ai 默认重试策略一致）。
_EMPTY_MARKERS = (
    "completed response with no content",
    "EMPTY_RESPONSE",
)


def classify_bridge_error(text: str) -> str:
    """把 provider 错误文本分成 quota（额度用尽，不重试）/ rate（可重试）/
    empty（空回复，可短退避重试）/ unknown。

    纯函数，可离线单测。额度优先：额度文案里常顺带出现 429 字样，
    先判额度再判速率，避免把"额度用尽"误判成"普通限流去重试"。
    empty 独立于 quota/rate（文本无交集），放最后不影响既有优先级。
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
    if any(m in text for m in _EMPTY_MARKERS):
        return "empty"
    return "unknown"


def _raise_classified(prefix: str, raw: str, http_status=None):
    """按分类抛对应用错：quota→不重试，rate→退避重试，empty→短退避重试，
    unknown→维持旧行为（永久错）。"""
    kind = classify_bridge_error(raw)
    if kind == "quota":
        raise LLMQuotaExhaustedError(f"QUOTA_EXHAUSTED: {prefix}{raw[:300]}", http_status=http_status)
    if kind == "rate":
        raise LLMRetryableError(f"{prefix}{raw[:300]}", retry_after_s=30.0)
    if kind == "empty":
        raise LLMRetryableError(f"{prefix}{raw[:300]}", retry_after_s=5.0)
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
        return self._post_llm(model, thinking_level, max_tokens, tools,
                              {"prompt": prompt})

    def call_messages(self, model, thinking_level, messages, tools, max_tokens):
        """messages 形态单轮调用（v15.10 tool loop 用）：不经过 BaseLLMClient 的
        重试（loop 层自己掌握重试/降级），直接调 _post_llm。返回 (text, meta)，
        meta 可能带 tool_calls。"""
        return self._post_llm(model, thinking_level, max_tokens, tools,
                              {"messages": messages})

    def _post_llm(self, model, thinking_level, max_tokens, tools, body_extra):
        payload = {
            "model": model,
            "max_tokens": max_tokens,
            "session_id": _BRIDGE_SESSION_ID,
        }
        payload.update(body_extra)
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
        pending_tool_calls = []  # v15.10：{event:"tool_calls", calls:[{id,name,arguments}]}
        # v15.12f（2026-09-14）：空响应诊断用的事件计数。此前 "no content" 抛错时
        # finish_reason / usage / 各类事件计数全被丢掉，只留一句完全不透明的话——
        # 实测 deepseek-flash@max 间歇触发（8 样本里 3 次），无法判断到底是
        # 「模型只吐了 reasoning 就停」「provider 回空」还是「delta 被谁吞了」。
        ev_counts = {}
        ev_bad_json = 0
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
                ev_bad_json += 1
                continue
            if not isinstance(ev_obj, dict):
                continue  # 非对象载荷（如纯字符串行）直接跳过，不炸整轮
            event_type = ev_obj.get("event")
            ev_counts[event_type] = ev_counts.get(event_type, 0) + 1
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
            elif event_type == "tool_calls":
                # v15.10：插件把本轮累积的完整 tool call 一次性发出；
                # 执行由调用方（tool_loop）负责，这里只透传。
                for tc in ev_obj.get("calls") or []:
                    if isinstance(tc, dict) and tc.get("name"):
                        pending_tool_calls.append({
                            "id": str(tc.get("id") or f"call_{len(pending_tool_calls)}"),
                            "name": str(tc["name"]),
                            "arguments": tc.get("arguments") or {},
                        })

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
        if not text and not pending_tool_calls:
            # 2026-09-06（评审回执）：无 finish_error 的纯空完成（如 adapter 升级后
            # 不再翻译 empty）也走 empty 路径——文案与 _EMPTY_MARKERS 对齐，
            # 让 bench _is_empty_response 能识别（短退避重试 + empty 状态）。
            # v15.10：纯 tool_calls 无文本是正常中间态（模型先调工具再写），不抛错。
            # v15.12f：诊断紧跟原文案之后（下游按子串匹配，且调用方会 [:200] 截断，
            # 放在末尾会被切掉）。判别口径：
            #   reasoning 有计数 / text 为 0 → 模型只 Reasoning 就停了（reasoning-only）
            #   events 全 0                      → provider 根本没发事件（真空回）
            #   usage.outputTokens>0 而 text=0   → delta 在转发层被吞（桥接 bug）
            raise LLMRetryableError(
                "DSH bridge completed response with no content"
                f" [finish={finish_reason} ev={ev_counts} out_tok={(usage or {}).get('outputTokens')}"
                f" bad_json={ev_bad_json}]",
                retry_after_s=5.0,
            )

        meta_out = CallMeta(
            finish_reason=finish_reason,
            elapsed_s=meta.get("elapsed_s", 0.0),
            usage=usage,
            timeout_kind=meta.get("timeout_kind"),
            http_status=meta.get("http_status"),
            transport="dsh_bridge",
        ).as_dict()
        if pending_tool_calls:
            meta_out["tool_calls"] = pending_tool_calls
        return text, meta_out


def tool_exec_url(bridge_url: str = None) -> str:
    """tool-exec 端点 URL（v15.10）：与 llm-stream 同源，把尾巴换掉。"""
    base = (bridge_url or _default_dsh_bridge_url()).strip()
    if base.endswith("/llm-stream"):
        return base[: -len("/llm-stream")] + "/tool-exec"
    return base.rstrip("/") + "/tool-exec"


def tool_exec(name: str, args: dict, timeout_s: float = DEFAULT_TOOL_EXEC_TIMEOUT_S,
              bridge_url: str = None) -> str:
    """执行一个 DSH 宿主工具调用（v15.10）：POST /api/council/tool-exec。
    插件侧只放行 allowlist 内的只读工具（web_search/web_fetch），经 ctx.web 执行；
    这里只做传输 + 错误分类。返回结果文本；失败按分类抛错。"""
    url = tool_exec_url(bridge_url)
    payload = {"name": name, "args": args or {},
               "session_id": _BRIDGE_SESSION_ID}
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(url, data=data, headers={
        "Content-Type": "application/json",
        "User-Agent": "dsh-council-bridge/1.0",
    }, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=timeout_s) as resp:
            raw = resp.read(256 * 1024).decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        try:
            body = e.read(2000).decode("utf-8", "replace")
        except Exception:
            body = ""
        kind = classify_bridge_error(body)
        if kind == "quota":
            raise LLMQuotaExhaustedError(
                f"QUOTA_EXHAUSTED: tool-exec HTTP {e.code}: {body[:300]}",
                http_status=e.code)
        if kind == "rate" or e.code == 429 or 500 <= e.code < 600:
            raise LLMRetryableError(f"tool-exec HTTP {e.code}: {body[:300]}",
                                    retry_after_s=10.0)
        raise LLMPermanentError(f"tool-exec HTTP {e.code}: {body[:300]}",
                                http_status=e.code)
    except (TimeoutError, OSError) as e:
        raise LLMRetryableError(f"tool-exec network error: {e}"[:200],
                                retry_after_s=10.0)
    try:
        obj = json.loads(raw)
    except json.JSONDecodeError:
        raise LLMPermanentError(f"tool-exec 非 JSON 响应：{raw[:200]}")
    if not isinstance(obj, dict) or not obj.get("ok"):
        err = str((obj or {}).get("error", "unknown"))[:300]
        kind = classify_bridge_error(err)
        if kind == "rate":
            raise LLMRetryableError(f"tool-exec error: {err}", retry_after_s=10.0)
        raise LLMPermanentError(f"tool-exec error: {err}")
    return str(obj.get("result", ""))


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