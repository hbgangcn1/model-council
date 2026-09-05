"""LLM 调用：v15.6+ 走 LLMClient 抽象（DSH bridge / OpenAI-compat / Anthropic-compat）。

v15.5 兼容路径仍保留：MiniMax → 直连 anthropic、其它 → 直连 deepseek。
LLM 客户端失败时自动 fallback 到兼容路径（保持 v15.5 benchmark 可跑）。"""
import json
import os
import sys
import time
import urllib.request
import urllib.error
from pathlib import Path

from . import config, sandbox

# v15.6+ LLMClient 抽象：stream_llm 是 orchestrator 包内模块，
# 直接 import 即可，不需要 sys.path hack。
from orchestrator import stream_llm  # noqa: E402

UA = "curl/8.0"

# ---------------- HTTP ----------------

def _http(url: str, headers: dict, payload: dict, timeout: int = 900):
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(url, data=data, headers=headers, method="POST")
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))

def _retry(fn, tries=3):
    last = None
    for i in range(tries):
        try:
            return fn()
        except urllib.error.HTTPError as e:
            body = e.read().decode("utf-8", "replace")[:200] if e.fp else ""
            if e.code == 429:
                wait = min(60, 10 * (2 ** i))
                time.sleep(wait)
                last = Exception(f"HTTP 429 (retry {i + 1})")
                continue
            raise Exception(f"HTTP {e.code}: {body}")
        except Exception as e:
            last = e
            if i < tries - 1:
                time.sleep(5 * (i + 1))
    raise last if last else Exception("unknown failure")

# ---------------- 无工具调用 ----------------

def call_deepseek(model: str, thinking: dict, prompt: str, max_tokens: int):
    keys = config.load_api_keys()
    headers = {"Authorization": f"Bearer {keys['DEEPSEEK_API_KEY']}",
               "Content-Type": "application/json", "User-Agent": UA}
    payload = {"model": model, "max_tokens": max_tokens,
               "messages": [{"role": "user", "content": prompt}],
               "temperature": 0, "stream": False}
    if thinking:
        payload.update(thinking)  # v15.5：wire 来自 host-side tier-bridge（thinking/reasoning_effort）
    t0 = time.time()
    data = _retry(lambda: _http(config.DEEPSEEK_URL, headers, payload))
    msg = data["choices"][0]["message"]
    text = (msg.get("content") or "").strip()
    if not text and msg.get("reasoning_content"):
        text = f"[thinking]\n{msg['reasoning_content']}"
    meta = {"elapsed_s": round(time.time() - t0, 1),
            "finish_reason": data["choices"][0].get("finish_reason"),
            "usage": data.get("usage", {})}
    return text, meta

def call_minimax(model: str, thinking: dict, prompt: str, max_tokens: int):
    keys = config.load_api_keys()
    headers = {"x-api-key": keys["MINIMAX_CN_API_KEY"],
               "anthropic-version": "2023-06-01",
               "Content-Type": "application/json", "User-Agent": UA}
    payload = {"model": model, "max_tokens": max_tokens,
               "messages": [{"role": "user", "content": prompt}],
               "temperature": 0}
    if thinking:
        payload.update(thinking)  # v15.5：wire 来自 host-side tier-bridge（anthropic thinking 形态）
    t0 = time.time()
    data = _retry(lambda: _http(config.MINIMAX_URL, headers, payload))
    parts = []
    for block in data.get("content", []):
        if block.get("type") == "text":
            parts.append(block.get("text", ""))
    text = "\n".join(parts).strip()
    if not text:
        for block in data.get("content", []):
            if block.get("type") == "thinking":
                text = f"[thinking]\n{block.get('thinking', '')}"
                break
    meta = {"elapsed_s": round(time.time() - t0, 1),
            "finish_reason": data.get("stop_reason"),
            "usage": data.get("usage", {})}
    return text, meta

# ---------------- 工具循环 ----------------

TOOLS = [
    {"name": "read_file", "description": "Read a file inside the sandbox directory.",
     "params": {"path": {"type": "string", "description": "Relative path, e.g. README.md"}}},
    {"name": "list_dir", "description": "List files in a sandbox directory.",
     "params": {"path": {"type": "string", "description": "Relative dir path, '' for root"}}},
    {"name": "search_content", "description": "Search file names and contents for a pattern.",
     "params": {"pattern": {"type": "string", "description": "Case-insensitive substring"},
                "path": {"type": "string", "description": "Relative dir path, '' for all"}}},
]

def _exec_tool(name, args, root):
    if name == "read_file":
        return sandbox.read_file(root, args.get("path", ""))
    if name == "list_dir":
        return json.dumps(sandbox.list_dir(root, args.get("path", "")), ensure_ascii=False)
    if name == "search_content":
        return json.dumps(sandbox.search_content(root, args.get("pattern", ""),
                                                 args.get("path", "")), ensure_ascii=False)
    raise ValueError(f"unknown tool {name}")

def run_deepseek_with_tools(model: str, thinking: dict, prompt: str,
                            max_tokens: int, sandbox_root, max_rounds: int = 20):
    keys = config.load_api_keys()
    headers = {"Authorization": f"Bearer {keys['DEEPSEEK_API_KEY']}",
               "Content-Type": "application/json", "User-Agent": UA}
    messages = [{"role": "user", "content": prompt}]
    openai_tools = [{"type": "function", "function": {
        "name": t["name"], "description": t["description"],
        "parameters": {"type": "object", "properties": t["params"], "required": []}}}
        for t in TOOLS]
    sandbox.reset_counts()
    final_text = ""
    for _ in range(max_rounds):
        payload = {"model": model, "max_tokens": max_tokens, "messages": messages,
                   "temperature": 0, "stream": False,
                   "tools": openai_tools}
        if thinking:
            payload.update(thinking)
        data = _retry(lambda: _http(config.DEEPSEEK_URL, headers, payload))
        msg = data["choices"][0]["message"]
        tcs = msg.get("tool_calls") or []
        if not tcs:
            final_text = (msg.get("content") or "").strip()
            break
        messages.append(msg)
        for tc in tcs:
            try:
                args = json.loads(tc["function"].get("arguments", "{}"))
            except json.JSONDecodeError:
                args = {}
            try:
                result = _exec_tool(tc["function"]["name"], args, sandbox_root)
            except Exception as e:
                result = f"[error] {e}"
            messages.append({"role": "tool", "tool_call_id": tc["id"],
                             "content": str(result)[:6000]})
    return final_text, sandbox.get_call_count()

def run_minimax_with_tools(model: str, thinking: dict, prompt: str,
                           max_tokens: int, sandbox_root, max_rounds: int = 20):
    keys = config.load_api_keys()
    headers = {"x-api-key": keys["MINIMAX_CN_API_KEY"],
               "anthropic-version": "2023-06-01",
               "Content-Type": "application/json", "User-Agent": UA}
    messages = [{"role": "user", "content": prompt}]
    mm_tools = [{"name": t["name"], "description": t["description"],
                 "input_schema": {"type": "object", "properties": t["params"]}}
                for t in TOOLS]
    sandbox.reset_counts()
    final_text = ""
    for _ in range(max_rounds):
        payload = {"model": model, "max_tokens": max_tokens, "messages": messages,
                   "temperature": 0, "tools": mm_tools}
        if thinking:
            payload.update(thinking)
        data = _retry(lambda: _http(config.MINIMAX_URL, headers, payload))
        content = data.get("content", [])
        tool_uses = [b for b in content if b.get("type") == "tool_use"]
        if not tool_uses:
            for b in content:
                if b.get("type") == "text":
                    final_text += b.get("text", "")
            break
        messages.append({"role": "assistant", "content": content})
        results = []
        for tu in tool_uses:
            try:
                r = _exec_tool(tu["name"], tu.get("input", {}), sandbox_root)
            except Exception as e:
                r = f"[error] {e}"
            results.append({"type": "tool_result", "tool_use_id": tu["id"],
                            "content": str(r)[:6000]})
        messages.append({"role": "user", "content": results})
    return final_text.strip(), sandbox.get_call_count()

# ---------------- v15.6 L3 桥接：所有 model 优先经 host-bridge /api/council/llm-stream ----------------

# budget_tokens → reasoning level 映射（anthropic 协议专有，host level 字段接收字符串）
_BUDGET_TO_LEVEL = {1024: "minimal", 2048: "low", 8192: "medium", 16384: "high"}


def _wire_to_level(thinking_wire: dict) -> str:
    """从 model-tier-bridge.json 的 wire dict 提取 level 字符串。
    zai/deepseek: {"reasoning_effort": "low"} → "low"
    anthropic: {"thinking": {"type": "disabled"}} → "off"
    anthropic: {"thinking": {"type": "enabled", "budget_tokens": 2048}} → "low"
    其它（兜底）→ ""（host-bridge uses model/provider 默认档位）
    """
    if not thinking_wire or not isinstance(thinking_wire, dict):
        return ""
    if "reasoning_effort" in thinking_wire:
        return str(thinking_wire["reasoning_effort"])
    t = thinking_wire.get("thinking")
    if isinstance(t, dict):
        if t.get("type") == "disabled":
            return "off"
        if t.get("type") == "enabled":
            return _BUDGET_TO_LEVEL.get(int(t.get("budget_tokens", 0)), "")
    return ""


def call_via_dsh_bridge(model: str, thinking_wire: dict, prompt: str, max_tokens: int):
    """v15.6+：通过 LLMClient 抽象调任意 model（默认走 host-bridge，端点由环境变量配置）。

    v15.6+ 客户端默认包含 host-bridge plugin（DSH_BRIDGE_URL 设置时），
    也可切到 OpenAI-compat / Anthropic-compat（看环境变量）。
    host-bridge unavailable时 fallback 到原 minimax / deepseek 直连（v15.5 兼容路径）。
    """
    level = _wire_to_level(thinking_wire)
    try:
        text, meta = stream_llm.call_stream(
            model=model, thinking_level=level, prompt=prompt, max_tokens=max_tokens
        )
        # 适配 bench 期望的 meta 字段：call_stream 返回的 finish_reason 是 "stop"/"error"，
        # 而 bench 期望 data["choices"][0].get("finish_reason") 风格字符串。
        if meta.get("finish_reason") is None:
            meta["finish_reason"] = "stop" if meta.get("error") else "error"
        # provider 错误（429 等）对非 deepseek/MiniMax 模型必须抛异常（fallback 无意义）
        if meta.get("finish_reason") == "error":
            raise Exception(f"LLMClient error: {meta.get('error') or meta.get('transport')}")
        return text, meta
    except Exception as e:
        # LLMClient 失败 → fallback 只对 deepseek/MiniMax 系有意义
        # （glm-5.3-flash 等走 host-bridge 的模型在旧 API 无路由，429 时应重试而非降级）
        if model.startswith("MiniMax"):
            return call_minimax(model, thinking_wire, prompt, max_tokens)
        if model.startswith("deepseek"):
            return call_deepseek(model, thinking_wire, prompt, max_tokens)
        raise


# v15.6+：tool-use case 走 host-bridge（之前走 deepseek 直连 → glm-5.3-flash HTTP 400），
# 通过 messages + tools 多轮循环实现 function calling。URL 默认来自 stream_llm 模块常量，
# 可通过 DSH_BRIDGE_URL 环境变量覆盖。
DSH_BRIDGE_URL = stream_llm.DEFAULT_DSH_URL


def _stream_sse_lines(url: str, headers: dict, payload: dict,
                      timeout: int = 900):
    """v15.6：独立 SSE 解析（不复用 stream_llm._stream_lines 因为那是私有符号）。

    返回 (events: list[str], meta: dict)。
    - events 是逐行字符串（'data: {...}' 形式），调用方解析每行。
    - meta 含 timeout_kind / http_status / http_body / elapsed_s。
    """
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(url, data=data, headers=headers, method="POST")
    events = []
    last_byte_ts = time.time()
    start_ts = time.time()
    timeout_kind = None
    http_status = None
    http_body = None
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            for raw in resp:
                line = raw.decode("utf-8", "replace").rstrip("\n")
                if line:
                    events.append(line)
                    last_byte_ts = time.time()
                elif time.time() - last_byte_ts > timeout:
                    timeout_kind = "idle"
                    break
                if time.time() - start_ts > timeout:
                    timeout_kind = "total"
                    break
    except urllib.error.HTTPError as e:
        http_status = e.code
        try:
            http_body = e.read(300).decode("utf-8", "replace")[:300]
        except Exception:
            pass
        timeout_kind = f"http:{e.code}"
    except (TimeoutError, OSError) as e:
        if time.time() - last_byte_ts >= timeout:
            timeout_kind = "idle"
        else:
            timeout_kind = f"network:{type(e).__name__}"
    meta = {
        "timeout_kind": timeout_kind,
        "elapsed_s": round(time.time() - start_ts, 1),
        "n_events": len(events),
    }
    if http_status is not None:
        meta["http_status"] = http_status
        meta["http_body"] = http_body
    return events, meta


def call_via_dsh_bridge_with_tools(model, thinking_wire, prompt, max_tokens,
                                    openai_tools, sandbox_root, max_rounds=20,
                                    rate_limit_retries=3):
    """v15.6 L3 tool-use 桥接：host-bridge 接受 messages + tools 数组多轮循环。

    协议：
      请求: POST /api/council/llm-stream
        body = { model, messages: [...], tools: [...], level, max_tokens }
      响应: SSE events
        {event: "text", delta: "..."} 累积
        {event: "tool_calls", calls: [{id, name, arguments}, ...]}  ← 触发本轮执行 tool
        {event: "finish", reason: ...}  正常结束

    Python 端循环：
      1. 发请求
      2. 解析 SSE events
      3. 如果有 tool_calls → 调本地 _exec_tool → 把 tool 结果作为 role=tool message 塞回 messages
      4. 继续下一轮（直到 model 输出纯文本 finish）

    v15.6 429 限流重试：整个多轮循环包在 rate_limit_retries 层——
    Z.AI Lite 并发上限 ~2，3 worker 并发时部分 case 会 429。429 时退避
    30s/60s/120s 后整体重试（不是 runner.py 的立即 retry，那个对限流无效）。
    """
    import urllib.request  # 已在模块顶部 import
    level = _wire_to_level(thinking_wire)
    last_rate_limit_error = None

    for attempt in range(rate_limit_retries + 1):
        try:
            return _dsh_bridge_tool_loop(model, level, prompt, max_tokens,
                                          openai_tools, sandbox_root, max_rounds)
        except _DshRateLimitError as e:
            last_rate_limit_error = e
            if attempt < rate_limit_retries:
                wait = 30 * (2 ** attempt)  # 30s / 60s / 120s
                print(f"      [429 rate-limit] {e.message[:80]} — 退避 {wait}s 后重试（{attempt+1}/{rate_limit_retries}）", flush=True)
                time.sleep(wait)
            else:
                raise Exception(f"host-bridge rate limit exceeded after {rate_limit_retries} retries: {e.message}") from e
    raise Exception(f"host-bridge rate limit: {last_rate_limit_error}")  # unreachable


class _DshRateLimitError(Exception):
    """host-bridge provider 429 限流（区别于其它 provider 错误，需要退避重试而非立即失败）。"""
    def __init__(self, message):
        super().__init__(message)
        self.message = message


def _dsh_bridge_tool_loop(model, level, prompt, max_tokens,
                          openai_tools, sandbox_root, max_rounds):
    """单次多轮 tool loop（不带限流重试；429 时抛 _DshRateLimitError）。"""
    messages = [{"role": "user", "content": prompt}]
    sandbox.reset_counts()
    final_text = ""
    rounds_used = 0
    last_error = None

    for _round in range(max_rounds):
        rounds_used = _round + 1
        body = {
            "model": model,
            "messages": messages,
            "max_tokens": max_tokens,
            "level": level,
        }
        if openai_tools:
            body["tools"] = openai_tools
        data = json.dumps(body).encode("utf-8")
        req = urllib.request.Request(DSH_BRIDGE_URL, data=data,
                                    headers={"Content-Type": "application/json", "User-Agent": UA},
                                    method="POST")
        # 用本文件内的 _stream_sse_lines（独立实现，避免依赖 stream_llm 的私有符号）
        events, meta = _stream_sse_lines(DSH_BRIDGE_URL,
                                         {"Content-Type": "application/json", "User-Agent": UA},
                                         body)

        # 解析 events
        text = ""
        tool_calls = []
        finish_kind = None
        for line in events:
            if not line.startswith("data:"):
                continue
            chunk = line[5:].strip()
            if chunk == "[DONE]" or not chunk:
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
            elif ev == "tool_calls":
                tool_calls = d.get("calls") or []
            elif ev == "error":
                last_error = d.get("message") or "bridge error"
            elif ev == "finish":
                # v15.6 修复：finish.reason.kind == "error" 表示 provider 错误（429 限流/5xx 等）。
                # 之前只看 event:"error"（HTTP 层），漏掉 finish 事件里的 error kind，
                # 导致 429 被静默吞掉 → 产物空但 status=done → 误判为并发 race condition。
                fr = d.get("reason")
                if isinstance(fr, dict):
                    finish_kind = fr.get("kind")
                    if finish_kind == "error":
                        failure = fr.get("failure") or {}
                        last_error = failure.get("message") or last_error or "provider error"
                elif isinstance(fr, str):
                    finish_kind = fr

        # 检查 timeout/error（meta 键以 _stream_sse_lines docstring 为准：
        # timeout_kind/http_status/http_body/elapsed_s/n_events——没有 bridge_error，
        # 那是历史残留键，全仓已无构造处，别加回来）
        if meta.get("timeout_kind") and meta.get("timeout_kind") not in ("stop", None):
            raise Exception(f"host-bridge {meta.get('timeout_kind')}: {meta.get('http_body') or 'unknown'}")

        # v15.6：provider 错误分流——429 限流抛 _DshRateLimitError（退避重试），其它直接抛
        if finish_kind == "error" or (last_error and not text and not tool_calls):
            err_msg = last_error or "unknown"
            if "1302" in err_msg or "429" in err_msg or "速率限制" in err_msg or "RATE_LIMIT" in err_msg:
                raise _DshRateLimitError(err_msg)
            raise Exception(f"host-bridge provider error: {err_msg}")

        if not tool_calls:
            # model 输出纯文本，本轮结束；保留所有轮 text（包括前几轮的"思考/前缀"）
            if text and not final_text:
                final_text = text
            elif text:
                final_text = (final_text + "\n" + text) if final_text else text
            break
        # 累积本轮 text（即使有 tool_calls，model 也可能先输出 text 再调 tool）
        if text:
            final_text = (final_text + "\n" + text) if final_text else text

        # 构造 assistant message（含 tool_calls）追加到 messages
        messages.append({
            "role": "assistant",
            "content": text or None,
            "tool_calls": [{
                "id": tc.get("id", f"call_{_round}_{i}"),
                "type": "function",
                "function": {
                    "name": tc["name"],
                    "arguments": tc["arguments"] if isinstance(tc["arguments"], str) else json.dumps(tc["arguments"]),
                },
            } for i, tc in enumerate(tool_calls)],
        })

        # 执行每个 tool，把结果作为 role=tool message 追加
        for tc in tool_calls:
            tc_id = tc.get("id", "unknown")
            tc_name = tc.get("name", "")
            tc_args_str = tc["arguments"] if isinstance(tc["arguments"], str) else json.dumps(tc["arguments"])
            try:
                tc_args = json.loads(tc_args_str) if tc_args_str else {}
            except json.JSONDecodeError:
                tc_args = {}
            try:
                result = _exec_tool(tc_name, tc_args, sandbox_root)
            except Exception as e:
                result = f"[error] {e}"
            messages.append({
                "role": "tool",
                "tool_call_id": tc_id,
                "content": str(result)[:6000],
            })
    else:
        # max_rounds 用完，model 还在调 tool — 视为超时
        raise Exception(f"host-bridge tool loop exceeded max_rounds={max_rounds} (model still calling tools)")

    if last_error and not final_text:
        raise Exception(f"host-bridge error: {last_error}")
    return final_text, sandbox.get_call_count()


# ---------------- 统一入口 ----------------

def run_case(cand_model: str, cand_thinking: str, prompt: str,
             use_tools: bool = False, sandbox_root=None):
    """统一入口：v15.6 优先走 host-bridge（含 tool-use case 的多轮 function calling）。
    2026-08-27：OpenRouter（stealth/ox-alpha）路由随 provider 退役移除。
    v15.6 L3 修复：use_tools=True 路径也走 host-bridge（之前走 deepseek 直连，
    glm-5.3-flash 跑 T1/T2/T3 tool-use case 全部 HTTP 400 失败；现经 host-bridge
    通过 pi-ai + zai-coding-cn 端点真打 Z.AI）。
    """
    thinking = config.thinking_param(cand_model, cand_thinking)
    max_tokens = config.max_tokens_for(cand_model)  # v15.5：模型能力上限，不再按档位
    if use_tools:
        # v15.6 L3 桥接：tool-use case 走 host-bridge（多轮 messages + tools function calling）
        # host-bridge unavailable时 fallback 到原 minimax/deepseek 直连（让旧 benchmark 仍能跑）
        openai_tools = [{"type": "function", "function": {
            "name": t["name"], "description": t["description"],
            "parameters": {"type": "object", "properties": t["params"], "required": []}}}
            for t in TOOLS]
        try:
            text, call_count = call_via_dsh_bridge_with_tools(
                cand_model, thinking, prompt, max_tokens, openai_tools, sandbox_root)
            return text, {"tool_calls": call_count}
        except Exception:
            # v15.6 修复：fallback 只对 deepseek/MiniMax 系模型有意义（它们在旧 API 有路由）。
            # 其它模型（glm-5.3-flash 等走 host-bridge 的）fallback 只会报 model not found——
            # 429 限流时正确做法是抛异常让 runner.py retry（等限流恢复），而不是静默降级。
            if cand_model.startswith("MiniMax") or cand_model.startswith("deepseek"):
                is_mm = cand_model.startswith("MiniMax")
                if is_mm:
                    return run_minimax_with_tools(
                        cand_model, thinking, prompt, max_tokens, sandbox_root)
                return run_deepseek_with_tools(
                    cand_model, thinking, prompt, max_tokens, sandbox_root)
            raise
    return call_via_dsh_bridge(cand_model, thinking, prompt, max_tokens)
