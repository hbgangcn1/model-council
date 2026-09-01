"""selector.py 单元测试：评分/护栏优先级/熔断三态/峰谷/成本表/tie-break/缺项回退。

注意：熔断与护栏事件文件用 COUNCIL_CIRCUIT_FILE / COUNCIL_GUARD_EVENTS 隔离，
避免污染生产数据（真实 circuit-state.json / guardrail-events.jsonl 不被测试触碰）。
"""
import datetime
import json
import os
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

_tmp = tempfile.mkdtemp(prefix="council-test-")
os.environ["COUNCIL_CIRCUIT_FILE"] = os.path.join(_tmp, "circuit-state.json")
os.environ["COUNCIL_GUARD_EVENTS"] = os.path.join(_tmp, "guardrail-events.jsonl")

from orchestrator import selector  # noqa: E402

FAILED = []

def check(name, cond):
    if cond:
        print(f"  ✓ {name}")
    else:
        print(f"  ✗ {name}")
        FAILED.append(name)

def test_estimate_tokens():
    print("[estimate_tokens]")
    check("中文约1.5字/token", 90 <= selector.estimate_tokens("你好世界" * 20) <= 200)
    check("英文约4字符/token", 10 <= selector.estimate_tokens("hello world" * 5) <= 60)

def test_time_factor():
    print("[time_factor 峰谷]")
    peak = datetime.datetime(2026, 8, 25, 10, 0)   # 周二 10:00 高峰
    off = datetime.datetime(2026, 8, 25, 21, 0)    # 周二 21:00 低谷
    weekend = datetime.datetime(2026, 8, 23, 10, 0)  # 周日
    check("工作日高峰=1.0", selector.time_factor("deepseek-official", peak) == 1.0)
    check("工作日低谷=0.5", selector.time_factor("deepseek-official", off) == 0.5)
    check("周末全天低谷", selector.time_factor("deepseek-official", weekend) == 0.5)
    check("无峰谷配置的 provider=1.0", selector.time_factor("minimax-cn", peak) == 1.0)

def test_norm_percent():
    print("[norm_percent 百分比语义守卫 H3]")
    check("整数百分比原样（96→96）", selector.norm_percent(96) == 96.0)
    check("小数比例自动修复（0.96→96）", selector.norm_percent(0.96) == 96.0)
    check("越界收敛（150→100）", selector.norm_percent(150) == 100.0)
    check("负数收敛（-5→0）", selector.norm_percent(-5) == 0.0)

def test_quota_factor():
    print("[quota_factor 多窗口取最紧]")
    b1 = {"minimax-cn:5h": 90, "minimax-cn:week": 90}
    check("充裕=1.0", selector.quota_factor("minimax-cn", b1) == 1.0)
    b2 = {"minimax-cn:5h": 10, "minimax-cn:week": 90}
    q2 = selector.quota_factor("minimax-cn", b2)
    check("5h吃紧周充裕→取最紧(>1.0)", q2 > 1.0)
    b3 = {"minimax-cn:5h": 0, "minimax-cn:week": 90}
    check("耗尽=inf", selector.quota_factor("minimax-cn", b3) == float("inf"))
    b4 = {}
    check("查不到→保守2.0", selector.quota_factor("minimax-cn", b4) == 2.0)
    # 小数语义自动修复后同样耗尽
    b5 = {"minimax-cn:5h": 0.005, "minimax-cn:week": 90}
    check("小数比例 0.005 → 修复为 0.5% 后仍耗尽", selector.quota_factor("minimax-cn", b5) == float("inf"))

def test_thinking_multiplier():
    print("[thinking 系数表驱动 H3]")
    check("DeepSeek high=2.3（报告实测口径）",
          selector.thinking_multiplier("deepseek-official", "deepseek-v4-pro", "high") == 2.3)
    check("DeepSeek off=1.0",
          selector.thinking_multiplier("deepseek-official", "deepseek-v4-pro", "off") == 1.0)
    check("MiniMax medium 走 minimax 表",
          selector.thinking_multiplier("minimax-cn", "MiniMax-M3", "medium") == 1.6)

def test_cost_cache_hit():
    print("[成本：缓存命中计价 H3]")
    peak = datetime.datetime(2026, 8, 25, 10, 0)
    no_cache = selector.effective_cost_cny("deepseek-official", "deepseek-v4-flash", "off",
                                           100000, 10000, now=peak, balance={},
                                           cache_hit_rate=0.0)
    with_cache = selector.effective_cost_cny("deepseek-official", "deepseek-v4-flash", "off",
                                             100000, 10000, now=peak, balance={},
                                             cache_hit_rate=0.8)
    check("有缓存命中成本低于无缓存", with_cache["cost_cny"] < no_cache["cost_cny"])
    check("CostContext 带单位字段齐全", "inputCnyPerMTok" in no_cache
          and "cache_hit_rate" in no_cache and "thinking_mult" in no_cache)
    check("costUsd 按汇率换算", no_cache["costUsd"] is not None
          and abs(no_cache["costUsd"] - no_cache["cost_cny"] / no_cache["usdToCny"]) < 1e-5)

def test_score_and_guards():
    print("[评分+护栏]")
    caps = {"models": {
        "m1__off": {"baseModel": "m1", "thinking": "off", "provider": "deepseek-official",
                    "stable": True,
                    "capabilities": {"reasoning": {"score": 9.0}, "code": {"score": 8.0}}},
        "m2__off": {"baseModel": "m2", "thinking": "off", "provider": "minimax-cn",
                    "stable": True,
                    "capabilities": {"reasoning": {"score": 7.0}, "code": {"score": 8.0}}},
        "m1__high": {"baseModel": "m1", "thinking": "high", "provider": "deepseek-official",
                     "stable": True,
                     "capabilities": {"reasoning": {"score": 9.5}, "code": {"score": 8.5}}},
    }}
    ctx = {"lambda_": 0.5, "mu": 0, "used_providers": set(), "banned_models": set(),
           "balance": {}, "epsilon": 0.0,
           "est_input_tokens": 500, "est_output_tokens": 200,
           "now": datetime.datetime(2026, 8, 25, 10, 0)}
    task = {"reasoning": 0.8, "code": 0.2}
    ranked = selector.select(task, ctx, caps)
    check("有有效候选", len(ranked) >= 3)
    check("m1@high 排第一(能力最强)", ranked[0][0] == "m1__high")
    # 自验禁令
    ctx2 = {**ctx, "banned_models": {"m1"}}
    ranked2 = selector.select(task, ctx2, caps)
    check("自验禁令剔除 m1 系", all(r[1]["baseModel"] != "m1" for r in ranked2))
    # 余额耗尽
    ctx3 = {**ctx, "balance": {"deepseek-official:balance": 0.0,
                               "deepseek-official:monthly_estimate": 100.0}}
    ranked3 = selector.select(task, ctx3, caps)
    check("余额耗尽剔除 deepseek 系", all(r[1]["baseModel"] != "m1" for r in ranked3))

def test_guard_priority_and_events():
    print("[护栏优先级 + 事件日志 H4]")
    caps = {"models": {
        "u1__off": {"baseModel": "u1", "thinking": "off", "provider": "deepseek-official",
                    "stable": False, "identityUnknown": True,
                    "capabilities": {"reasoning": {"score": 9.0}}},
    }}
    ctx = {"lambda_": 0.5, "mu": 0, "used_providers": set(), "banned_models": set(),
           "balance": {}, "epsilon": 0.0, "est_input_tokens": 500,
           "est_output_tokens": 200, "now": datetime.datetime(2026, 8, 25, 10, 0),
           "run_id": "test-run-001"}
    # 同一候选同时触发 identity_unknown + unstable → 只报优先级更高的 identity_unknown
    ok, reason = selector.passes_guards(caps["models"]["u1__off"], ctx)
    check("冲突取最高优先级 reason（identity_unknown）", not ok and reason == "identity_unknown")
    events_file = Path(os.environ["COUNCIL_GUARD_EVENTS"])
    check("护栏剔除落 guardrail-events.jsonl", events_file.exists())
    if events_file.exists():
        rows = [json.loads(l) for l in events_file.read_text(encoding="utf-8").splitlines() if l.strip()]
        hit = next((r for r in rows if r.get("guard_name") == "identity_unknown"), None)
        check("事件含 guard_name/model@thinking/reason_code",
              hit is not None and hit["reason_code"] == "identity_unknown"
              and "ts" in hit and "snapshot_ref" in hit)
        # H 项：run_id/阈值/实测值 三要素齐全
        check("事件含 run_id", hit is not None and hit.get("run_id") == "test-run-001")
        check("事件含 threshold（身份未知护栏）",
              hit is not None and isinstance(hit.get("threshold"), dict)
              and "identityUnknown" in hit["threshold"])
        check("事件含 measured（实测 identityUnknown=true）",
              hit is not None and isinstance(hit.get("measured"), dict)
              and hit["measured"].get("identityUnknown") is True)
    # self_verify_ban 的阈值/实测值
    ctx2 = {**ctx, "banned_models": {"u1"}}
    caps2 = {"models": {
        "u1__off": {"baseModel": "u1", "thinking": "off", "provider": "deepseek-official",
                    "stable": True,
                    "capabilities": {"reasoning": {"score": 9.0}}},
    }}
    ok2, reason2 = selector.passes_guards(caps2["models"]["u1__off"], ctx2)
    check("自验禁令触发", not ok2 and reason2 == "self_verify_ban")
    rows2 = [json.loads(l) for l in events_file.read_text(encoding="utf-8").splitlines() if l.strip()]
    hit2 = next((r for r in rows2 if r.get("guard_name") == "self_verify_ban"), None)
    check("自验禁令事件含 bannedModels 阈值",
          hit2 is not None and hit2["threshold"].get("bannedModels") == ["u1"])

def test_circuit():
    print("[熔断器三态]")
    m = "test-model"
    for _ in range(3):
        selector.record_failure(m)
    check("3次失败→open", selector._circuit_state(m) in ("open", "half_open"))
    selector.record_success(m)
    check("成功→closed", selector._circuit_state(m) == "closed")

def test_circuit_half_open_and_backoff():
    print("[熔断器半开探测 + 指数退避 M4]")
    m = "probe-model"
    for _ in range(3):
        selector.record_failure(m)
    st = selector._load_circuit()[m]
    check("熔断带 open_until", st["state"] == "open" and st["open_until"] > time.time())
    # 半开：冷却到期前只允许一个探测
    st["open_until"] = time.time() - 1
    selector._write_circuit({m: st})
    check("冷却到期→half_open", selector._circuit_state(m) == "half_open")
    check("第一个探测获准", selector._probe_begin(m) is True)
    check("第二个探测被拒（同一时刻单探测）", selector._probe_begin(m) is False)
    # 探测失败 → 回 open 且退避翻倍
    selector.record_failure(m)
    st2 = selector._load_circuit()[m]
    check("探测失败→open", st2["state"] == "open")
    check("退避 ≥ 2×base（指数退避，容差 2s）",
          st2["open_until"] - time.time() >= 2 * selector.CIRCUIT_BASE_BACKOFF_S - 2)
    # 探测成功 → closed
    st2["open_until"] = time.time() - 1
    selector._write_circuit({m: st2})
    check("再次冷却→half_open", selector._circuit_state(m) == "half_open")
    selector._probe_begin(m)
    selector.record_success(m)
    check("探测成功→closed", selector._circuit_state(m) == "closed")

def test_tie_break():
    print("[tie-break：同分按成本→延迟 L1]")
    a = ("a__off", {"baseModel": "a", "provider": "x", "latencyP50Ms": 2000}, 8.0, {"cost_cny": 0.05})
    b = ("b__off", {"baseModel": "b", "provider": "y", "latencyP50Ms": 1000}, 8.0, {"cost_cny": 0.01})
    c = ("c__off", {"baseModel": "c", "provider": "z", "latencyP50Ms": 1000}, 8.0, {"cost_cny": 0.01})
    ranked = sorted([a, b, c], key=selector._rank_key)
    check("同分成本低者在前", ranked[0][0] in ("b__off", "c__off"))
    check("成本相同延迟低者在前", ranked[0][0] == "b__off")
    check("高分恒在前", sorted([a, ("d__off", {}, 9.0, {"cost_cny": 1.0})],
                               key=selector._rank_key)[0][0] == "d__off")

def test_fallback():
    print("[缺项回退 + fallback 标记 L2]")
    caps = {"models": {
        "f1__off": {"baseModel": "f1", "thinking": "off", "provider": "deepseek-official",
                    "stable": True,
                    "capabilities": {"reasoning": {"score": 9.0, "samples": 31},
                                     "code": {"score": 8.0, "samples": 31}}},
    }}
    ctx = {"lambda_": 0.0, "mu": 0, "used_providers": set(), "banned_models": set(),
           "balance": {}, "epsilon": 0.0, "est_input_tokens": 500,
           "est_output_tokens": 200, "now": datetime.datetime(2026, 8, 25, 10, 0)}
    # 任务维度含 chinese（该模型无此维度）→ 回退模型均值 + fallback=true
    task = {"reasoning": 0.5, "code": 0.2, "chinese": 0.3}
    ranked = selector.select(task, ctx, caps)
    check("有候选", len(ranked) == 1)
    meta = ranked[0][3].get("scoreMeta", {})
    check("缺项标记 fallback=true", meta.get("fallback") is True)
    check("fallback_dims 含 chinese", "chinese" in meta.get("fallback_dims", []))
    check("confidence 字段存在", isinstance(meta.get("confidence"), float))

def test_epsilon():
    print("[ε-greedy]")
    ranked = [("a", {}, 10, {}), ("b", {}, 9, {}), ("c", {}, 8, {})]
    out = selector.epsilon_greedy(list(ranked), {"epsilon": 1.0})
    check("探索时长度不变", len(out) == 3)
    check("探索时榜首稳定", out[0][0] == "a")

def main():
    test_estimate_tokens()
    test_time_factor()
    test_norm_percent()
    test_quota_factor()
    test_thinking_multiplier()
    test_cost_cache_hit()
    test_score_and_guards()
    test_guard_priority_and_events()
    test_circuit()
    test_circuit_half_open_and_backoff()
    test_tie_break()
    test_fallback()
    test_epsilon()
    print()
    if FAILED:
        print(f"❌ {len(FAILED)} 项失败: {FAILED}")
        sys.exit(1)
    print("✅ 全部通过")

if __name__ == "__main__":
    main()
