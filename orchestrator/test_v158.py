# -*- coding: utf-8 -*-
"""v15.8 回归测试（全离线，不调 API，不花钱）：
A. 限流分类：额度用尽 vs 可重试；B. 活性门 + 进度回调 + 额度熔断。
用法：python orchestrator/test_v158.py
"""
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from orchestrator import judge_drift  # noqa: E402
from orchestrator.llm_transports import dsh_bridge  # noqa: E402
from orchestrator.llm_client import (  # noqa: E402
    BaseLLMClient, LLMQuotaExhaustedError, LLMRetryableError, LLMPermanentError)

FAILED = []


def check(name, cond):
    if cond:
        print(f"  PASS {name}")
    else:
        print(f"  FAIL {name}")
        FAILED.append(name)


def test_classify():
    print("[classify_bridge_error]")
    c = dsh_bridge.classify_bridge_error
    check("1008 insufficient balance -> quota", c("1008 insufficient balance") == "quota")
    check("2056 usage limit exceeded -> quota", c("2056 usage limit exceeded, wait next 5-hour window") == "quota")
    check("余额不足 -> quota", c("账户余额不足，请充值") == "quota")
    check("额度用尽且带429 -> quota优先", c("429 额度用尽，本窗口已耗尽") == "quota")
    check("1002 rate limit -> rate", c("1002 rate limit exceeded") == "rate")
    check("纯429 -> rate", c("429 Too Many Requests") == "rate")
    check("1302 Z.AI -> rate", c("1302 您的账户已达到速率限制") == "rate")
    check("1039 token limit -> rate", c("1039 token limit exceeded") == "rate")
    check("500 -> rate", c("500 internal error") == "rate")
    check("400 参数错 -> unknown", c("400 invalid param: max_tokens") == "unknown")


def test_raise_types():
    print("[_raise_classified]")
    try:
        dsh_bridge._raise_classified("p: ", "2056 usage limit exceeded")
        check("quota抛错", False)
    except LLMQuotaExhaustedError as e:
        check("quota抛LLMQuotaExhaustedError", True)
        check("quota带QUOTA_EXHAUSTED前缀", "QUOTA_EXHAUSTED" in str(e))
        check("quota是Permanent子类（天然不重试）", isinstance(e, LLMPermanentError))
    try:
        dsh_bridge._raise_classified("p: ", "1002 rate limit")
        check("rate抛错", False)
    except LLMRetryableError:
        check("rate抛LLMRetryableError", True)
    try:
        dsh_bridge._raise_classified("p: ", "400 invalid param")
        check("unknown抛错", False)
    except LLMPermanentError as e:
        check("unknown维持旧行为LLMPermanentError", not isinstance(e, LLMQuotaExhaustedError))


def test_quota_fail_fast():
    print("[BaseLLMClient quota不重试]")
    class Q(BaseLLMClient):
        def __init__(self):
            super().__init__(transport_name="t", max_retries=3)
            self.calls = 0
        def _do_call(self, *a, **k):
            self.calls += 1
            raise LLMQuotaExhaustedError("QUOTA_EXHAUSTED: no money")
    q = Q()
    t0 = time.monotonic()
    try:
        q.call_stream("m", "low", "hi", 10)
        check("quota应抛错", False)
    except LLMQuotaExhaustedError:
        check("quota直接上抛类型不变", True)
    check("quota只调1次（零重试）", q.calls == 1)
    check("quota耗时<2s（无退避sleep）", time.monotonic() - t0 < 2.0)


def _run_do_call(events):
    orig = dsh_bridge._stream_sse_lines
    dsh_bridge._stream_sse_lines = lambda *a, **k: (events, {})
    try:
        return dsh_bridge.DSHBridgeClient()._do_call("m", "low", "hi", 10)
    finally:
        dsh_bridge._stream_sse_lines = orig


def test_finish_error_kind():
    print("[finish.reason.kind=error 不再吞错]")
    try:
        _run_do_call(['data: {"event":"finish","reason":{"kind":"error","failure":{"message":"2056 usage limit exceeded"}}}'])
        check("finish-quota应抛错", False)
    except LLMQuotaExhaustedError:
        check("finish-quota抛额度错", True)
    try:
        _run_do_call(['data: {"event":"finish","reason":{"kind":"error","failure":{"message":"1002 rate limit"}}}'])
        check("finish-rate应抛错", False)
    except LLMRetryableError:
        check("finish-rate抛可重试", True)
    text, meta = _run_do_call(['data: {"event":"text","delta":"hello"}',
                               'data: {"event":"finish","reason":"stop"}'])
    check("正常finish照常返回", text == "hello" and meta["finish_reason"] == "stop")
    # 线上实测：sseWrite 用 JSON.stringify 包哨兵，流尾是带引号的 data: "[DONE]"
    text2, _ = _run_do_call(['data: {"event":"text","delta":"hi"}',
                             'data: {"event":"finish","reason":{"kind":"stop"}}',
                             'data: "[DONE]"'])
    check("带引号[DONE]哨兵不炸整轮", text2 == "hi")


def test_liveness_stall():
    print("[活性门：hang住不 murder 整轮]")
    orig = judge_drift.call_stream
    import time as _t
    def hang(*a, **k):
        _t.sleep(5)
        return ("{}", {})
    judge_drift.call_stream = hang
    try:
        items = [{"id": f"G{i}", "task": "t", "answer": "a", "rubric": "r"} for i in range(4)]
        t0 = time.monotonic()
        out = judge_drift.score_items(items, "MiniMax-M3", "medium", max_workers=2,
                                      max_runtime_s=60.0, pass_timeout_s=0.3)
        el = time.monotonic() - t0
        check("4题全标stalled", all((r.get("stalled") or r["error"] == "stalled_no_progress") for r in out))
        check(f"活性门提前退出（{el:.1f}s << 60s backstop）", el < 10.0)
    finally:
        judge_drift.call_stream = orig


def test_quota_fuse_and_progress():
    print("[额度熔断 + 进度回调]")
    orig = judge_drift.call_stream
    calls = []
    def quota_fail(model, thinking, prompt, max_tokens):
        raise Exception("QUOTA_EXHAUSTED: MiniMax 5h 窗口耗尽")
    judge_drift.call_stream = quota_fail
    try:
        def cb(stage, label, done, total, ok, quota, extra=None):
            calls.append((stage, label, done, total, ok, quota))
        items = [{"id": f"G{i}", "task": "t", "answer": "a", "rubric": "r"} for i in range(6)]
        t0 = time.monotonic()
        out = judge_drift.score_items(items, "MiniMax-M3", "medium", max_workers=3,
                                      max_runtime_s=60.0, stage="main", progress_cb=cb,
                                      pass_timeout_s=5.0)
        el = time.monotonic() - t0
        check("剩题标quota_aborted", any(r["error"] == "quota_aborted" for r in out))
        check(f"熔断快速结束（{el:.1f}s）", el < 10.0)
        check("进度回调带模型@档位", any(c[1] == "MiniMax-M3@medium" and c[0] == "main" for c in calls))
        check("进度done递增", [c[2] for c in calls] == sorted(c[2] for c in calls) and calls[-1][2] >= 3)
    finally:
        judge_drift.call_stream = orig


def main():
    test_classify()
    test_raise_types()
    test_quota_fail_fast()
    test_finish_error_kind()
    test_liveness_stall()
    test_quota_fuse_and_progress()
    print()
    if FAILED:
        print(f"FAIL {len(FAILED)}: {FAILED}")
        sys.exit(1)
    print("ALL GREEN")


if __name__ == "__main__":
    main()
