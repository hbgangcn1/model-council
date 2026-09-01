"""v15.6 L3 桥接：Python orchestrator 通过 host-bridge /api/council/llm-stream 调任意 model。

host-bridge internals走 pi-ai 处理 wire/auth/retry/协议差异；Python 只关心"调 LLM 拿到 text+usage"。

保留的接口：
- call_stream(model, thinking_level, prompt, max_tokens) → (text, meta)
- meta 含 finish_reason / usage / timeout_kind / elapsed_s / n_events 等（与 v15.5 兼容）

host-bridge endpoint契约（详细见 host-bridge plugin/index.js 的 handleLlmStream）：
- POST /api/council/llm-stream
  请求体：{ model, level?, prompt, max_tokens?, system?, temperature? }
  响应头：200 + text/event-stream
  错误响应：4xx/5xx + JSON { error, code, provider, model }
  SSE events: {event: text|reasoning|usage|finish|error|warning} + [DONE]
"""
import json
import time
import urllib.error
import urllib.request

try:  # 包内导入；直接执行（judge_drift 等脚本）退回顶层导入
    from . import config_loader
except ImportError:
    import config_loader  # type: ignore

IDLE_TIMEOUT_S = 180       # 180s 无字节视为 idle（v15.3 元评审：与之前 180s 一致）
TOTAL_TIMEOUT_S = 1800     # 总兜底超时 30 分钟（v15.3 元评审）

# host-bridge default listens 3080（同主机 IPC；v15.6 设计选择 localhost 无鉴权）
DEFAULT_DSH_URL = "http://127.0.0.1:3080/api/council/llm-stream"


def _stream_lines(url, headers, payload, idle_timeout=IDLE_TIMEOUT_S,
                  total_timeout=TOTAL_TIMEOUT_S):
    """通用 SSE 流式读取：返回 (events, meta)。
    events 是逐行字符串列表（含 "data: ..." 与空行）；meta 含超时/活动/网络错误。
    被 bridge_stream 复用——所有 provider 协议差异都在 host-bridge handles，Python 只读 SSE。
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
        # v15.3：HTTP 4xx/5xx 显式记录状态码与响应体（便于诊断 401/500/502/504）
        http_status = e.code
        try:
            http_body = e.read(300).decode("utf-8", "replace")[:300]
        except Exception:
            pass
        timeout_kind = f"http:{e.code}"
    except (TimeoutError, OSError) as e:
        if time.time() - last_byte_ts >= idle_timeout:
            timeout_kind = "idle"
        else:
            timeout_kind = f"network:{type(e).__name__}"
            network_error = str(e)[:200]
    meta = {"timeout_kind": timeout_kind, "elapsed_s": round(time.time() - start_ts, 1),
            "n_events": len(events), "last_byte_age_s": round(time.time() - last_byte_ts, 1)}
    if http_status is not None:
        meta["http_status"] = http_status
        meta["http_body"] = http_body
    if network_error:
        meta["network_error"] = network_error
    return events, meta


def bridge_stream(model: str, thinking_level: str, prompt: str, max_tokens: int):
    """v15.6 L3 核心：POST 到 host-bridge /api/council/llm-stream，host-bridge internals走 pi-ai 调任意 provider。
    返回 (text, meta)；与 v15.5 deepseek_stream/minimax_stream 接口完全兼容，
    调用方（council.py 的 call_stream 路径）零改动。
    """
    keys = config_loader.api_keys()
    dsh_url = keys.get("DSH_BRIDGE_URL", DEFAULT_DSH_URL)
    headers = {"Content-Type": "application/json", "User-Agent": "host-bridge plugin-bridge/1.0"}
    payload = {
        "model": model,
        "prompt": prompt,
        "max_tokens": max_tokens,
    }
    if thinking_level:
        payload["level"] = thinking_level

    events, meta = _stream_lines(dsh_url, headers, payload)

    text = ""
    reasoning = ""
    finish = None
    usage = None
    error_event = None
    for line in events:
        if not line.startswith("data:"):
            continue
        chunk = line[5:].strip()
        if chunk == "[DONE]":
            break
        if not chunk:
            continue
        try:
            d = json.loads(chunk)
        except json.JSONDecodeError:
            continue
        if not isinstance(d, dict):
            continue
        ev = d.get("event")
        if ev == "text":
            text += d.get("delta", "")
        elif ev == "reasoning":
            reasoning += d.get("delta", "")
        elif ev == "usage":
            usage = d.get("usage") or {}
        elif ev == "finish":
            finish_reason = d.get("reason")
            # v15.6：host-bridge maps pi-ai 的 FinishReason 包成 {kind:'stop'|...}；兼容两种
            if isinstance(finish_reason, dict):
                finish = finish_reason.get("kind") or finish_reason.get("reason")
                # v15.6 修复：finish.kind == "error"（429 限流等 provider 错误）也转 finish_reason=error，
                # 之前只看 event:"error"（HTTP 层），429 被静默吞掉 → text 空
                if finish == "error":
                    failure = finish_reason.get("failure") or {}
                    error_event = {
                        "code": failure.get("code", "PROVIDER_ERROR"),
                        "message": failure.get("message", "provider error"),
                    }
            else:
                finish = finish_reason
        elif ev == "error":
            error_event = d
        elif ev == "warning":
            # 工具调用被忽略；记录但不影响流程
            meta.setdefault("warnings", []).append(d.get("message"))
        # block-start / block-end 等其它 event 忽略

    # meta 字段名与 v15.5 兼容（call_stream 调用方依赖这些）
    meta["finish_reason"] = finish
    meta["reasoning_len"] = len(reasoning)
    meta["text_len"] = len(text)
    if usage:
        # host-bridge end usage 字段是 TokenUsage（具体字段名见 dsh-llm/lib/types/types.d.ts），
        # 标准字段：promptTokens / completionTokens / cacheReadTokens / totalTokens。
        # 容错映射：兼容 input/output 命名（pi-ai provider 差异）。
        pt = usage.get("promptTokens") or usage.get("inputTokens") or usage.get("input_tokens") or 0
        ct = usage.get("completionTokens") or usage.get("outputTokens") or usage.get("output_tokens") or 0
        cr = usage.get("cacheReadTokens") or usage.get("cacheRead") or usage.get("cached_tokens") or 0
        meta["usage"] = {
            "promptTokens": pt,
            "completionTokens": ct,
            "cacheHitTokens": cr,
            "totalTokens": pt + ct,
        }
    if error_event:
        # 流式错误：转成统一的 finish_reason=error + 错误消息写入 meta
        meta["finish_reason"] = "error"
        meta["bridge_error"] = {
            "code": error_event.get("code"),
            "message": error_event.get("message"),
            "provider": error_event.get("provider"),
            "model": error_event.get("model"),
        }
    return text, meta


def call_stream(model: str, thinking_level: str, prompt: str, max_tokens: int):
    """v15.6 L3 统一入口：所有 model 都走 host-bridge接；不再按 model 前缀分发到具体 provider。
    host-bridge internals用 pi-ai 处理所有协议差异（zai/deepseek/anthropic/openai 等）。
    接口签名与 v15.5 兼容，council.py / benchmark 调用方零改动。
    """
    return bridge_stream(model, thinking_level, prompt, max_tokens)