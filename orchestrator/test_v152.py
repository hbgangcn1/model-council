"""v15.2 元评审修复的回归测试（2026-08-24 P0-2/P0-4/P0-5/P1-1/P1-2/P1-3/P2-1/P2-2）。

pytest 风格；所有文件副作用通过 monkeypatch 指向 tmp 目录，不触碰生产数据。
"""
import datetime
import json
import os
import sys
import tempfile
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from orchestrator import selector, terminator, pairwise, token_profiles, update_capabilities, budget  # noqa: E402


# ---------- 公共 fixtures ----------

def _caps():
    return {
        "revision": 4,
        "models": {
            "deepseek-v4-pro__off": {"baseModel": "deepseek-v4-pro", "thinking": "off",
                                     "provider": "deepseek-official", "stable": True,
                                     "identityUnknown": False,
                                     "capabilities": {"reasoning": {"score": 9.0, "samples": 30}}},
            "deepseek-v4-pro__max": {"baseModel": "deepseek-v4-pro", "thinking": "max",
                                     "provider": "deepseek-official", "stable": True,
                                     "identityUnknown": False,
                                     "capabilities": {"reasoning": {"score": 9.0, "samples": 30}}},
            "MiniMax-M3__low": {"baseModel": "MiniMax-M3", "thinking": "low",
                                "provider": "minimax-cn", "stable": True,
                                "identityUnknown": False,
                                "capabilities": {"reasoning": {"score": 8.0, "samples": 30}}},
            "unknown-model__low": {"baseModel": "unknown-model", "thinking": "low",
                                   "provider": "unknown", "stable": False,
                                   "identityUnknown": True,
                                   "capabilities": {"reasoning": {"score": 9.0, "samples": 30}}},
        },
    }


def _ctx(**kw):
    base = {"lambda_": 1.0, "mu": 0.001, "used_providers": set(),
            "banned_models": set(), "balance": {}, "epsilon": 0.0,
            "est_input_tokens": 800, "est_output_tokens": 400,
            "now": datetime.datetime(2026, 8, 25, 21, 0), "run_id": "test-run",
            "caps_revision": 4}
    base.update(kw)
    return base


TV = {"reasoning": 1.0}


# ---------- P0-2：静态预过滤 + 档位→thinking 硬约束 ----------

def test_prefilter_identity_unknown_and_unstable():
    ranked = selector.select(TV, _ctx(), _caps())
    cids = [r[0] for r in ranked]
    assert "unknown-model__low" not in cids          # identityUnknown + stable=false 预过滤
    assert all(r[2] is not None for r in ranked)         # 过滤发生在评分前


def test_v154_no_thinking_rank_cap():
    """v15.4：maxThinkingRank 已删除——每个档位视作不同模型，完全由 selector 自主。
    （旧 test_tier_thinking_hard_constraint 语义反转：max 档不再被档位上限剔除。）"""
    ranked = selector.select(TV, _ctx(max_thinking_rank=1), _caps())  # 参数已无效果
    cids = [r[0] for r in ranked]
    assert "deepseek-v4-pro__max" in cids            # max 档参选（selector 自主）
    assert "deepseek-v4-pro__off" in cids


def test_prefilter_self_verify_ban():
    ranked = selector.select(TV, _ctx(banned_models={"MiniMax-M3"}), _caps())
    cids = [r[0] for r in ranked]
    assert "MiniMax-M3__low" not in cids                 # 自验禁令在评分前剔除
    assert "deepseek-v4-pro__off" in cids


def test_allowlist():
    ranked = selector.select(TV, _ctx(allowlist_ignored=True), _caps())  # allowlist 空=不启用
    assert ranked
    # 通过 params 覆盖测试 allowlist 需要在 params 文件里配——这里只验证空 allowlist 不误伤
    assert "deepseek-v4-pro__off" in [r[0] for r in ranked]


def test_guard_event_pool_summary(tmp_path, monkeypatch):
    """P0-2：预过滤只记一条 pool_excluded 汇总事件。"""
    events = tmp_path / "guard.jsonl"
    monkeypatch.setattr(selector, "GUARD_EVENTS", events)
    monkeypatch.setattr(selector, "_POOL_EVENTS_SEEN", {})  # v15.3 去重缓存：测试隔离
    selector.select(TV, _ctx(), _caps())
    lines = events.read_text(encoding="utf-8").strip().splitlines()
    summary = [json.loads(l) for l in lines if json.loads(l)["guard_name"] == "pool_excluded"]
    assert len(summary) == 1
    assert summary[0]["capabilitiesRevision"] == 4
    assert summary[0]["measured"]["excludedByReason"]["identity_unknown"] == 1


# ---------- P0-4：墙钟预算 + 成本三口径 ----------

def test_terminator_wall_budget_forced():
    st = terminator.RoundState(round_no=1)
    a, reason = terminator.decide(st, False, "h", 9.5, 0.01, 0.03,
                                  wall_elapsed_s=241, wall_budget_s=240)
    assert a == "forced"
    assert "墙钟" in reason


def test_terminator_wall_budget_not_hit():
    st = terminator.RoundState(round_no=1)
    a, _ = terminator.decide(st, False, "h", 9.5, 0.01, 0.03,
                             wall_elapsed_s=100, wall_budget_s=240)
    assert a in ("rework", "converged", "early_stop")


def test_base_cost_cny_same_shape_as_actual():
    """P0-3：base_cost_cny 不含 thinking/quota 规划系数，供对账同口径。"""
    info = selector.effective_cost_cny("deepseek-official", "deepseek-v4-pro", "max",
                                       1000, 100, now=datetime.datetime(2026, 8, 25, 21, 0),
                                       balance={"deepseek-official:balance": 100})
    base = info["base_cost_cny"]
    assert base is not None
    assert info["cost_cny"] == pytest.approx(base * info["thinking_mult"] * info["quota_factor"], rel=1e-6)


def test_v154_precheck_balance_report():
    """v15.4：预检改余额感知报告（不拒派，status 恒为 report）。"""
    st = [{"id": "t1", "inputChars": "你好世界" * 100}]
    out = budget.precheck(st, 0.15, default_cand="deepseek-v4-flash__low", max_rounds=5)
    assert out["maxRounds"] == 5
    # 期望轮数放大（min(maxIter,2) × 1.2 开销）应大于单轮
    out1 = budget.precheck(st, 0.15, default_cand="deepseek-v4-flash__low", max_rounds=1)
    assert out["estimatedCny"] > out1["estimatedCny"] * 1.5
    # v15.4：无论任务多大都不拒派——status 恒为 report，余额覆盖进入 balanceCoverage
    st3 = [{"id": "s" + str(i), "inputChars": "子任务" * 60} for i in range(3)]
    bal = {"deepseek-official:balance": 100, "deepseek-official:monthly_estimate": 1}
    out3 = budget.precheck(st3, 0.03, default_cand="deepseek-v4-flash__low", max_rounds=3, balance=bal)
    assert out3["status"] == "report", out3
    assert "deepseek-official" in (out3.get("balanceCoverage") or {}), out3


# ---------- P0-5：同源隔离 + pending diff 审批流 ----------

def test_reject_self_scored_feedback(tmp_path, monkeypatch):
    fb = tmp_path / "feedback.jsonl"
    fb.write_text("\n".join(json.dumps(r, ensure_ascii=False) for r in [
        {"run_id": "r1", "case_id": "s1", "model": "deepseek-v4-pro", "thinking": "off",
         "scoredBy": "deepseek-v4-pro", "verifierScore": 9.0, "success": True,
         "hardGateHit": False, "reworkTriggered": False,
         "taskVector": {"reasoning": 1.0}, "latency_ms": 5000, "cost_usd": 0.01, "usage": {}},
        {"run_id": "r2", "case_id": "s2", "model": "deepseek-v4-pro", "thinking": "off",
         "scoredBy": "MiniMax-M3", "verifierScore": 7.0, "success": True,
         "hardGateHit": False, "reworkTriggered": False,
         "taskVector": {"reasoning": 1.0}, "latency_ms": 9000, "cost_usd": 0.02, "usage": {}},
    ]), encoding="utf-8")
    monkeypatch.setattr(update_capabilities, "FEEDBACK", fb)
    runtime, contributing, rejected = update_capabilities._feedback_to_runtime_scores()
    assert rejected == 1                       # 同源行被拒
    assert "r2" in contributing
    assert "r1" not in contributing


def test_plan_runtime_telemetry(tmp_path):
    caps = _caps()
    rows = [
        {"success": True, "model": "deepseek-v4-pro", "thinking": "off",
         "latency_ms": 10000, "verifierScore": 8.5, "usage": {"promptTokens": 500, "completionTokens": 200},
         "cost_usd": 0.01},
        {"success": True, "model": "deepseek-v4-pro", "thinking": "off",
         "latency_ms": 20000, "verifierScore": 7.5, "usage": {"promptTokens": 300, "completionTokens": 100},
         "cost_usd": 0.03},
    ]
    new_caps = update_capabilities.plan_runtime_telemetry(caps, rows)
    rt = new_caps["models"]["deepseek-v4-pro__off"]["runtime"]
    assert rt["samples"] == 2
    assert rt["latencyP50Ms"] == 15000           # 平均值
    assert rt["avgVerifyScore"] == 8.0
    assert new_caps["models"]["deepseek-v4-pro__off"]["cost"]["avgInputTokens"] == 400
    # 其它模型不受影响
    assert new_caps["models"]["MiniMax-M3__low"].get("runtime", {}).get("samples", 0) == 0


def test_pending_diff_apply_flow(tmp_path, monkeypatch):
    caps_file = tmp_path / "capabilities.json"
    caps_file.write_text(json.dumps(_caps(), ensure_ascii=False), encoding="utf-8")
    fb = tmp_path / "feedback.jsonl"
    fb.write_text("\n".join(json.dumps(r, ensure_ascii=False) for r in [
        {"run_id": "r9", "case_id": "s1", "model": "deepseek-v4-pro", "thinking": "off",
         "scoredBy": "MiniMax-M3", "verifierScore": 9.0, "success": True,
         "hardGateHit": False, "reworkTriggered": False,
         "taskVector": {"reasoning": 1.0}, "latency_ms": 5000, "cost_usd": 0.01, "usage": {}},
    ]), encoding="utf-8")
    pending = tmp_path / "pending.json"
    monkeypatch.setattr(update_capabilities, "CAPS", caps_file)
    monkeypatch.setattr(update_capabilities, "FEEDBACK", fb)
    monkeypatch.setattr(update_capabilities, "PENDING", pending)
    monkeypatch.setattr(update_capabilities, "BASE", tmp_path)
    out = update_capabilities.pending_diff()
    assert out.get("pendingDiff") is True
    pend = json.loads(pending.read_text(encoding="utf-8"))
    assert pend["baseRevision"] == 4
    # 档案未变 → apply 成功
    res = update_capabilities.apply_pending()
    assert res.get("applied") is True
    assert json.loads(caps_file.read_text(encoding="utf-8"))["revision"] == 5
    # 再生成 pending 后改动档案 → apply 拒绝（baseHash 不匹配）
    update_capabilities.pending_diff()   # 基于 rev6 生成 pending
    caps_file.write_text(json.dumps({**_caps(), "revision": 7}, ensure_ascii=False), encoding="utf-8")
    res2 = update_capabilities.apply_pending()
    assert res2.get("applied") is False
    assert res2.get("reason") == "baseHash_mismatch"


def test_drift_pause(tmp_path, monkeypatch):
    jd = tmp_path / "judge-drift.json"
    jd.write_text(json.dumps({"drift": -0.8}, ensure_ascii=False), encoding="utf-8")
    monkeypatch.setattr(update_capabilities, "BASE", tmp_path)
    paused, info = update_capabilities._drift_paused()
    assert paused is True
    assert info["drift"] == -0.8
    jd.write_text(json.dumps({"drift": -0.2}, ensure_ascii=False), encoding="utf-8")
    paused2, _ = update_capabilities._drift_paused()
    assert paused2 is False


# ---------- P1-3：pairwise Elo ----------

def test_pairwise_compute_pure():
    rows = [
        {"run_id": "r1", "model": "A", "verifierScore": 9.0},
        {"run_id": "r1", "model": "B", "verifierScore": 7.0},
        {"run_id": "r2", "model": "A", "verifierScore": 6.0},
        {"run_id": "r2", "model": "B", "verifierScore": 8.0},
    ]
    ratings, samples, pairs = pairwise.compute(rows)
    assert pairs >= 2
    assert samples["A"] >= 2 and samples["B"] >= 2
    # A 与 B 各胜一局，Elo 应回归均值附近（对称）
    assert abs(ratings["A"] - ratings["B"]) < 40


def test_token_profiles_est_fallback(tmp_path, monkeypatch):
    pf = tmp_path / "profiles.json"
    monkeypatch.setattr(token_profiles, "OUT", pf)
    i, o = token_profiles.est_for("deepseek-v4-pro", "exec", 800, 400)
    assert (i, o) == (800, 400)                  # 冷启动回退启发式
    token_profiles.record("deepseek-v4-pro", "exec", 1000, 2000, 0)
    assert pf.exists()
    prof = json.loads(pf.read_text(encoding="utf-8"))
    assert prof["byModel"]["deepseek-v4-pro"]["exec"]["avgOut"] == 2000


# ---------- P1-2：judge prompt 哈希 ----------

def test_judge_prompt_hash_sensitive_to_rubric():
    from orchestrator import judge_drift
    g1 = {"version": 1, "items": [{"id": "G1", "rubric": "准确性与逻辑"}]}
    g2 = {"version": 1, "items": [{"id": "G1", "rubric": "准确性与逻辑（改）"}]}
    assert judge_drift._prompt_hash(g1) != judge_drift._prompt_hash(g2)


# ---------- P2-2：fx stale 等级 ----------

def test_fx_status_ok(tmp_path, monkeypatch):
    rates = tmp_path / "exchange-rates.json"
    rates.write_text(json.dumps({"usdToCny": 6.78, "stale": False, "staleReasons": []}),
                     encoding="utf-8")
    from orchestrator import fetch_exchange_rate
    monkeypatch.setattr(selector, "BASE", tmp_path)
    monkeypatch.setattr(fetch_exchange_rate, "RATES_FILE", rates)
    st = selector.fx_status()
    assert st["level"] == 0


def test_fx_status_halt(tmp_path, monkeypatch):
    from orchestrator import fetch_exchange_rate
    rates = tmp_path / "exchange-rates.json"
    rates.write_text(json.dumps({"usdToCny": 6.78, "stale": True,
                                 "publishDate": "2026-08-19 9:15",
                                 "staleReasons": ["publishDate_outdated"]}),
                     encoding="utf-8")
    monkeypatch.setattr(selector, "BASE", tmp_path)
    monkeypatch.setattr(fetch_exchange_rate, "RATES_FILE", rates)
    st = selector.fx_status()
    assert st["level"] == 2                   # 3 个交易日落后 → 停机
    assert st["tradingDaysBehind"] >= 3


# ---------- v15.3：rank 归一化（元评审 P0：同质化治理） ----------

def test_rank_normalize_restores_discrimination():
    """元评审实证：能力分同质化（9.5-10）时 Σ权重×能力分近似常数 → selector 退化。
    rank 归一化把每维分数映射到排序位置（0-10 均匀展开），恢复候选间区分度。"""
    caps = {
        "revision": 1,
        "models": {
            "a__off": {"baseModel": "a", "thinking": "off", "provider": "x", "stable": True,
                       "identityUnknown": False,
                       "capabilities": {"reasoning": {"score": 10.0, "samples": 30}}},
            "b__off": {"baseModel": "b", "thinking": "off", "provider": "y", "stable": True,
                       "identityUnknown": False,
                       "capabilities": {"reasoning": {"score": 9.9, "samples": 30}}},
            "c__off": {"baseModel": "c", "thinking": "off", "provider": "z", "stable": True,
                       "identityUnknown": False,
                       "capabilities": {"reasoning": {"score": 9.8, "samples": 30}}},
        },
    }
    ctx = {"lambda_": 0.0, "mu": 0.0, "used_providers": set(), "banned_models": set(),
           "balance": {}, "epsilon": 0.0, "est_input_tokens": 100, "est_output_tokens": 100,
           "run_id": "rank-test"}
    ranked = selector.select({"reasoning": 1.0}, ctx, caps)
    scores = {r[0]: round(r[2], 6) for r in ranked}
    # v15.4 百分制：第一名 100、最末名 1、中间 50.5（多样性加分等量；
    # paretoEnabled=true 给前沿候选 +0.1，断言放宽 ±0.3 容差）
    assert abs((scores["a__off"] - scores["b__off"]) - 49.5) <= 0.3
    assert abs((scores["b__off"] - scores["c__off"]) - 49.5) <= 0.3
    assert abs((scores["a__off"] - scores["c__off"]) - 99.0) <= 0.3


def test_rank_table_ties_average():
    """并列分数取平均排名：两个同分候选拿到相同的归一化分。"""
    caps = {
        "revision": 1,
        "models": {
            "a__off": {"baseModel": "a", "thinking": "off", "provider": "x", "stable": True,
                       "identityUnknown": False,
                       "capabilities": {"reasoning": {"score": 9.0, "samples": 30}}},
            "b__off": {"baseModel": "b", "thinking": "off", "provider": "x", "stable": True,
                       "identityUnknown": False,
                       "capabilities": {"reasoning": {"score": 9.0, "samples": 30}}},
            "c__off": {"baseModel": "c", "thinking": "off", "provider": "x", "stable": True,
                       "identityUnknown": False,
                       "capabilities": {"reasoning": {"score": 8.0, "samples": 30}}},
        },
    }
    table = selector.build_rank_table(caps["models"])
    # 升序排列：c(8.0) 最低 → 1 分；a/b(9.0) 并列第 1/2 位 → 平均排名 1.5 → 1+1.5/2*99 = 75.25（v15.4 百分制）
    assert table["reasoning"]["a__off"] == table["reasoning"]["b__off"] == 75.25
    assert table["reasoning"]["c__off"] == 1.0


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-q"]))
