"""终止策略（§6）：动态轮数收敛判据。纯函数模块。

v15.13 收敛终止契约（Robert 拍板 2026-09-14，取代 v15.5 的墙钟主导）：
- **增长枯竭是主判据**：本轮 S_r 未比"进入本轮前的最好成绩"高出 ε(delta=0.2) 即记一次
  未提升，连续 plateauRounds(2) 轮未提升 → early_stop。这就是"分数不再增加就结束"。
- θ（9.5）保留但**不要指望**：历史 S_r 最高只到 9.33，2026-08 之后无一次 converged。
- **墙钟取消**：从"收敛限制器"降级为"失控护栏"（runawayGuardS，默认 3 小时），
  只在病态失控时兜底；正常 run 由增长枯竭自然结束。
- **删除「硬门禁连续 3 轮未消除 → stalled」**：该判据实测触发 21 次（比墙钟 11 次还多），
  会砍掉仍在提升的 run，与"只追求最佳收敛"冲突。
- 保留「硬门禁同一问题连续 2 轮无改善 → stalled」——那是真正的原地打转。
- 成本 forced 已删（v15.4）：cost_so_far/budget_cap 仅作审计兼容。
防失控 = 增长枯竭 + 同问题 stalled + 余额耗尽护栏（selector 层）+ 失控护栏（极端兜底）。
"""
from dataclasses import dataclass, field

try:
    from . import params as params_mod
except ImportError:
    import params as params_mod  # type: ignore

DELTA = 0.2          # 最小有效提升 ε（params.terminator.delta 可覆盖）
PLATEAU = 2          # 连续未刷新最好成绩的轮数上限（params.terminator.plateauRounds 可覆盖）
WINDOW = 3           # 已废弃：旧「近 N 轮极差」窗口，保留仅为兼容


def _term_params() -> dict:
    return params_mod.load().get("terminator", params_mod.DEFAULTS["terminator"])


def _delta() -> float:
    """最小有效提升 ε：本轮分数要比此前最好值高出 ε 才算"还在进步"（v15.13 语义）。"""
    return float(_term_params().get("delta", DELTA))


def _plateau() -> int:
    """连续多少轮没刷新最好成绩就判枯竭（v15.13，默认 2）。"""
    return int(_term_params().get("plateauRounds", PLATEAU))


def _no_improve_streak(s_history: list, eps: float) -> int:
    """从 S_r 历史无状态推导「连续多少轮没刷新最好成绩」。

    v15.13：有效提升 = s[i] > max(s[0..i-1]) + eps。第一次出现永远算提升。
    无状态推导是有意为之——手工构造状态 / --resume 重建 / 单元测试都能算对。
    """
    streak = 0
    for i in range(1, len(s_history)):
        prior_best = max(s_history[:i])
        if s_history[i] > prior_best + eps:
            streak = 0
        else:
            streak += 1
    return streak


def best_round_index(s_history: list, eps: float) -> int:
    """返回「最有意义的一轮」（1-based），与 _no_improve_streak **同一判据**。

    v15.14 修：此前 council_v14 的循环用「s_r > best_s_r」（无 ε）更新 best_round，
    而枯竭判据用「s_r > 此前最好 + ε」。两者不一致会产生自相矛盾的结果——
    实测 S_r=[7.33,8.08,8.33,8.17,8.5]：第 5 轮 8.5 只比 8.33 高 0.17 < ε=0.2，
    枯竭判据判它"未提升"，而 best_round 却认了它，终止原因于是写成
    「连续 2 轮未刷新最好成绩」而轨迹末值恰恰是最大值。
    现在统一：只有比「当前最好」高出 ε 才算刷新，取最后一次有效刷新的那一轮。
    """
    if not s_history:
        return 0
    best_i = 0
    for i in range(1, len(s_history)):
        if s_history[i] > s_history[best_i] + eps:
            best_i = i
    return best_i + 1


def _window() -> int:
    """已废弃：旧「近 N 轮极差」判据的窗口。保留仅为兼容外部读取。"""
    return int(_term_params().get("window", WINDOW))


@dataclass
class RoundState:
    round_no: int = 1
    s_history: list = field(default_factory=list)   # 各轮归一化整体分
    hard_gate_failures: list = field(default_factory=list)  # 各轮硬门禁失败标记
    rework_topics: list = field(default_factory=list)  # 各轮 rework 清单主题哈希
    # v15.13 注：不再有 no_improve_streak 字段——"连续未刷新最好成绩"由 s_history
    # 无状态推导（见 _no_improve_streak）。这样手工构造状态、--resume 重建状态、
    # 单元测试三条路径都不会因为漏调 advance() 而算错。

def decide(state: RoundState, hard_gate_failed: bool, rework_topics_hash: str,
           theta_accept: float, cost_so_far: float = 0.0, budget_cap: float = 0.0,
           wall_elapsed_s: float = None, wall_budget_s: float = None) -> tuple:
    """返回 (action, reason)。action ∈ {converged, rework, early_stop, stalled, forced}。

    v15.13（2026-09-14 Robert 拍板：取消墙钟）判停顺序：
      失控护栏(病态兜底) → 硬门禁(同问题2轮) → θ 达标 → **增长枯竭** → rework

    两处关键改动：
      1. 墙钟从"收敛限制器"降级为"失控护栏"。此前它每 run 都触发（实测 terminated
         方式里 forced 11 次），把"还没收敛"当成"跑太久"处理，直接破坏收敛。
         现在 wall_budget_s 取一个极大的 runawayGuardS，只在病态失控时兜底。
      2. 增长枯竭判据由「近 window 轮极差 < δ」改为「**连续 K 轮未刷新最好成绩**」。
         旧判据不成立的原因：实测 S_r 轨迹是 ±0.5~1.0 的震荡（如 7.92,7.75,7.58,6.50,
         6.67），极差几乎不可能 <0.2；而 θ=9.5 历史最高只到 9.33，也够不到。
         两条质量判据都形同虚设，墙钟就成了事实上的唯一终止器。
         新判据直接实现"分数不再增加了就结束"：每轮需比**进入本轮前的最好成绩**高出
         ε 才算有效提升，连续 K 轮无效即枯竭。
    """
    delta = _delta()
    plateau = _plateau()

    # 0. 失控护栏（v15.13：不再是收敛判据！只在病态失控时兜底）
    #    调用方传进来的 wall_budget_s 现在是 runawayGuardS（默认 3 小时），
    #    正常 run 靠下面的"增长枯竭"自然结束，不会走到这里。
    if wall_budget_s and wall_elapsed_s is not None and wall_elapsed_s >= wall_budget_s:
        return ("forced", f"失控护栏触发：已运行 {wall_elapsed_s:.0f}s ≥ 兜底 {wall_budget_s:.0f}s"
                          "（非收敛判据，正常 run 不应触发；触发即说明枯竭判据失灵或真有病态循环）")

    # 1. 硬门禁失败 → 带清单返工；同问题连续 2 轮无改善 → stalled
    if hard_gate_failed:
        if state.round_no >= 2 and state.rework_topics and state.rework_topics[-1] == rework_topics_hash:
            return ("stalled", "同一问题连续 2 轮无改善（硬门禁）")
        # v15.13：删除「硬门禁连续 3 轮未消除 → stalled」。它和墙钟一样是"把没收敛当成
        # 该停"的判据——实测 stalled 触发 21 次，比墙钟还多，会砍掉仍在提升的 run。
        # 现在由"增长枯竭"统一负责：只要分数还在刷新最好成绩就继续返工，不再强制停。
        state.hard_gate_failures.append(True)
        state.rework_topics.append(rework_topics_hash)
        return ("rework", "硬门禁失败，带清单返工")

    # 本轮无硬门禁失败 → 记分
    s_r = _last_score(state)
    if s_r is None:
        # 首轮没有分数（数据缺失）→ 返工让 verifier 补分
        return ("rework", "首轮缺验证分，要求 verifier 补打分")

    # 2. 绝对阈值（θ）→ 收敛（保留；但历史最高只到 9.33，不要指望它）
    if s_r >= theta_accept:
        return ("converged", f"S_r={s_r} ≥ θ={theta_accept}")

    # 3. 增长枯竭（v15.13 核心判据）：连续 plateau 轮没刷新最好成绩 → 结束
    streak = _no_improve_streak(state.s_history, delta)
    if streak >= plateau:
        return ("early_stop",
                f"连续 {streak} 轮未刷新最好成绩（每轮需较此前最好值 +{delta}），"
                f"S_r 轨迹 {state.s_history}")
    return ("rework", "未达标且仍有提升空间，带清单增量返工")

def _last_score(state: RoundState):
    return state.s_history[-1] if state.s_history else None

def advance(state: RoundState, score: float = None):
    """进入下一轮：把本轮分数记入历史。

    v15.13：这里只记分——"连续未刷新最好成绩"由 _no_improve_streak 从 s_history 推导，
    不再维护额外字段（避免手工构造/续跑场景下漏更新导致判停失灵）。
    """
    if score is not None:
        state.s_history.append(score)
    state.round_no += 1
    return state
