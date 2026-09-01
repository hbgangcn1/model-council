"""终止策略（§6）：动态轮数收敛判据。纯函数模块。

v15.5 收敛终止契约（the maintainer decided 2026-08-25，见 DESIGN-v14.md §14-v15.5-A）：
- θ 三档统一 9.5（理论满分天花板——S_r ≈ verifier 平均打分，极少达到；多数 run 以增长枯竭收尾，converged 稀有属预期）；
- 增长枯竭：近 window（3）轮滑动窗口内 S_r 极差 < delta（0.2）→ early_stop；
- 删除全部轮数上限（maxIter/maxRounds 不再触发 forced）——只追求最佳收敛；
- 硬门禁：同一问题连续 2 轮无改善 → stalled；
- 墙钟预算（动态，见 wallBudget.*）：最高优先级 forced（技术防呆，防宿主超时丢结果）；
- 成本 forced 已删（v15.4）：cost_so_far/budget_cap 参数保留仅为审计兼容，不触发 forced。
防失控 = 增长枯竭 + 墙钟 + stalled + 余额耗尽护栏（selector 层）。
"""
from dataclasses import dataclass, field

try:
    from . import params as params_mod
except ImportError:
    import params as params_mod  # type: ignore

DELTA = 0.2          # 增长枯竭阈值（params.terminator.delta 可覆盖）
WINDOW = 3           # 增长枯竭滑动窗口轮数（params.terminator.window 可覆盖）


def _term_params() -> dict:
    return params_mod.load().get("terminator", params_mod.DEFAULTS["terminator"])


def _delta() -> float:
    return float(_term_params().get("delta", DELTA))


def _window() -> int:
    return int(_term_params().get("window", WINDOW))


@dataclass
class RoundState:
    round_no: int = 1
    s_history: list = field(default_factory=list)   # 各轮归一化整体分
    hard_gate_failures: list = field(default_factory=list)  # 各轮硬门禁失败标记
    rework_topics: list = field(default_factory=list)  # 各轮 rework 清单主题哈希

def decide(state: RoundState, hard_gate_failed: bool, rework_topics_hash: str,
           theta_accept: float, cost_so_far: float = 0.0, budget_cap: float = 0.0,
           wall_elapsed_s: float = None, wall_budget_s: float = None) -> tuple:
    """返回 (action, reason)。action ∈ {converged, rework, early_stop, stalled, forced}。
    v15.5（the maintainer decided）：无轮数上限；判停顺序 = 墙钟 → 硬门禁(stalled/rework) →
    θ 达标 → 近 3 轮窗口极差 < δ 增长枯竭 → 否则 rework。"""
    delta = _delta()
    window = _window()

    # 0. 墙钟预算（最高优先级——超时比任何质量指标都不可逆）
    if wall_budget_s and wall_elapsed_s is not None and wall_elapsed_s >= wall_budget_s:
        return ("forced", f"墙钟 {wall_elapsed_s:.0f}s ≥ 预算 {wall_budget_s:.0f}s")

    # 1. 硬门禁失败 → 必须返工；同问题连续 2 轮无改善 → stalled（v15.5 无轮数上限）
    if hard_gate_failed:
        if state.round_no >= 2 and state.rework_topics and state.rework_topics[-1] == rework_topics_hash:
            return ("stalled", "同一问题连续 2 轮无改善（硬门禁）")
        # v15.5b（试跑实测）：清单每轮变化时同清单判据失效 → 硬门禁连续 3 轮未消除即 stalled
        # （防无限返工；防失控 = 增长枯竭 + 墙钟 + stalled + 余额耗尽，无轮数上限）
        if len(state.hard_gate_failures) >= 2:
            return ("stalled", "硬门禁连续 3 轮未消除（rework 清单每轮变化），停止返工输出根因分析")
        state.hard_gate_failures.append(True)
        state.rework_topics.append(rework_topics_hash)
        return ("rework", "硬门禁失败，带清单返工")

    # 本轮无硬门禁失败 → 记分
    s_r = _last_score(state)
    if s_r is None:
        # 首轮没有分数（数据缺失）→ 返工让 verifier 补分
        return ("rework", "首轮缺验证分，要求 verifier 补打分")

    # 2. 绝对阈值（v15.5：θ=9.5 三档统一，理论天花板）→ 收敛
    if s_r >= theta_accept:
        return ("converged", f"S_r={s_r} ≥ θ={theta_accept}")

    # 3. 增长枯竭（v15.5：近 window 轮滑动窗口极差 < δ，防 ±δ 震荡永不停）
    recent = state.s_history[-window:]
    if len(recent) >= window:
        spread = max(recent) - min(recent)
        if spread < delta:
            return ("early_stop",
                    f"近 {window} 轮 S_r 极差 {spread:.2f} < δ={delta}，增长枯竭")

    return ("rework", "未达标且仍有提升空间，带清单增量返工")

def _last_score(state: RoundState):
    return state.s_history[-1] if state.s_history else None

def advance(state: RoundState, score: float = None):
    """进入下一轮：把本轮分数记入历史。"""
    if score is not None:
        state.s_history.append(score)
    state.round_no += 1
    return state
