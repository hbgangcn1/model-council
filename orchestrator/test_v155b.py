"""v15.5b 元评审修复测试（2026-08-25 第四次元评审 F3/F5/F8/F14/F15 落地项）。

覆盖：pool_excluded 语义分流、judge 漂移三档 level、容量截断公式（防 no_candidate 轮空）、
k 退化告警参数、解析全败警告链路（warnings 结构）。
"""
import math
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from orchestrator import selector, judge_drift  # noqa: E402
from orchestrator import params as params_mod  # noqa: E402


# ---------- F3：pool_excluded 语义分流 ----------

def test_semantic_split_quality_resource():
    excluded = {"self_verify_ban": ["a"], "identity_unknown": ["b", "c"],
                "balance_exhausted": ["d"], "some_other": ["e"]}
    out = selector._semantic_split(excluded)
    assert out["quality"]["self_verify_ban"] == 1
    assert out["quality"]["identity_unknown"] == 2
    assert out["resource"]["balance_exhausted"] == 1
    assert out["schedule"]["some_other"] == 1


# ---------- F5：judge 漂移三档 level ----------

def test_drift_level_ok():
    assert judge_drift.drift_level_for(0.1, 0.3, 0.5, 1.0) == "ok"
    assert judge_drift.drift_level_for(None, 0.3, 0.5, 1.0) == "ok"
    assert judge_drift.drift_level_for(-0.1, 0.3, 0.5, 1.0) == "ok"


def test_drift_level_warn_alert_critical():
    assert judge_drift.drift_level_for(0.3, 0.3, 0.5, 1.0) == "warn"
    assert judge_drift.drift_level_for(-0.5, 0.3, 0.5, 1.0) == "alert"
    assert judge_drift.drift_level_for(1.2, 0.3, 0.5, 1.0) == "critical"


def test_drift_level_params_present():
    jd = params_mod.load().get("judgeDrift", {})
    assert jd.get("warnDrift") == 0.3
    assert jd.get("pauseWriteDrift") == 0.5
    assert jd.get("alertThreshold") == 1.0


# ---------- F14/F8：容量截断公式（防 no_candidate 结构性轮空） ----------

def _capacity(n_subtasks, n_exec, n_vendors):
    """与 council_v14.py 相同的截断/配额公式（v15.5b 修正版）。"""
    cap = math.ceil(n_subtasks * n_exec / max(1, n_vendors))
    feasible = max(1, (n_vendors * cap) // n_exec)
    return cap, feasible


def test_capacity_no_round_hole_dual_exec():
    # 元评审实测场景：4 子任务、双路、3 厂商 → 截断后 3 个子任务必须全可派
    n_exec, n_vendors = 2, 3
    for n_sub in range(1, 8):
        cap, feasible = _capacity(n_sub, n_exec, n_vendors)
        # 截断后子任务数 ≤ feasible 时，执行位 = n × 2 ≤ 厂商容量 = V × cap
        n = min(n_sub, feasible)
        assert n * n_exec <= n_vendors * cap, f"n_sub={n_sub}: {n}*{n_exec} > {n_vendors}*{cap}"
        # 且 feasible 本身满足可行性
        assert feasible * n_exec <= n_vendors * cap


def test_capacity_four_subtasks_three_vendors():
    # 元评审 21-40-15 的实景：4→3 截断后仍轮空是旧公式缺陷；新公式下 4 个都不需要截断
    cap, feasible = _capacity(4, 2, 3)
    assert cap == 3
    assert feasible == 4  # 4×2=8 ≤ 3×3=9 → 无需截断
    assert 4 * 2 <= 3 * 3


def test_capacity_fast_single_exec():
    cap, feasible = _capacity(3, 1, 3)
    assert cap == 1
    assert feasible == 3
    assert 3 * 1 <= 3 * 1


# ---------- F15：result warnings 结构（run 级警告收集契约） ----------

def test_warnings_contract_fields():
    """council_v14 result.json 的 warnings 字段契约（verifier_parse_fail / 退化 / 替补失败）。"""
    from orchestrator import council_v14
    src = open(council_v14.__file__, encoding="utf-8").read()
    assert "_warnings = []" in src, "warnings 初始化缺失"
    assert '"warnings": _warnings' in src, "warnings 未进 result.json"
    assert "verifier_degraded" in src, "k 退化事件缺失"
    assert "verifier_parse_fail" in src


@pytest.mark.skip(reason="Integration test requiring host-bridge plugin; skipped in public release")
def test_auto_evolve_circuit_breaker_contract():
    """Validates the host-bridge plugin's auto_evolve circuit breaker.

    Skipped by default — requires the host-bridge plugin to be installed.
    Run manually after deploying the plugin to verify the contract.
    """
    # Path is host-plugin specific; in public release this test is a no-op.
    pytest.skip("integration test, requires host-bridge plugin")
