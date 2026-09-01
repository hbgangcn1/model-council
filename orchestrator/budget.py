"""余额感知预检（v15.4：不拒派，只报告；§14-v15.4-C）。

v15.4 核心哲学（the maintainer decided）：预检的作用是让选择模型时**考虑余额**——回答
「够不够钱跑」，而不是「超没超档位预算」。预算数字只做选择参考与报告，不做用量控制。

输出结构：
- estimatedCny：预计消耗（token EWMA + 校准系数，越跑越准）
- balanceCoverage：各 provider 余额/额度 vs 预计消耗的覆盖率（不足 100% 标注紧张）
- status 恒为 "report"（不再有 over/拒派）
"""
import json
from pathlib import Path

try:
    from . import selector
    from . import token_profiles
except ImportError:
    import selector  # type: ignore
    import token_profiles  # type: ignore

BASE = Path(__file__).resolve().parent.parent


def estimate_subtask_cost(cand_id: str, est_in: int, est_out: int, now=None, balance=None) -> float:
    """预估单个子任务成本（CNY）。单价缺失时返回 None（免费池）。"""
    caps = selector.load_capabilities()
    cand = caps.get("models", {}).get(cand_id)
    if not cand:
        return None
    provider = cand.get("provider")
    info = selector.effective_cost_cny(provider, cand["baseModel"], cand.get("thinking"),
                                       est_in, est_out, now=now, balance=balance)
    return info["cost_cny"]


def _coverage(estimated: float, balance: dict) -> dict:
    """各 provider 余额/额度 vs 预计消耗。返回 {provider: {kind, value, coveragePct}}。"""
    out = {}
    if not balance:
        return out
    # balance 快照 key 格式：deepseek-official:balance / minimax-cn:5h / minimax-cn:week（单位契约）
    known = {"deepseek-official": ("balance", balance.get("deepseek-official:balance")),
             "minimax-cn": ("quota", balance.get("minimax-cn:5h"))}
    for provider, (kind, val) in known.items():
        if not isinstance(val, (int, float)):
            continue
        if kind == "balance":
            pct = round(val / estimated * 100, 1) if estimated > 0 else None
            out[provider] = {"kind": "balance", "valueCny": round(float(val), 4),
                             "coveragePct": pct,
                             "tight": pct is not None and pct < 500}
        else:  # quota：百分比语义（96=96%）
            out[provider] = {"kind": "quota", "valuePct": float(val),
                             "coveragePct": None,
                             "tight": float(val) < 20}
    return out


def precheck(subtasks: list, budget_cap: float = 0.0, default_cand: str = None,
             est_out_tokens: int = 600, now=None, balance=None,
             safety_factor: float = 1.5, max_rounds: int = 3,
             overhead_factor: float = 1.2) -> dict:
    """v15.4：余额感知报告（不拒派）。budget_cap 参数仅为兼容保留，不参与判定。

    预计消耗 = Σ(子任务输入 token × 候选单价) × safety_factor × 期望轮数 × overhead；
    预检消费 token EWMA（est_out）与 per-(model,role) 校准系数（effective_cost_cny 内部）。"""
    total = 0.0
    unknown = 0
    per = []
    for st in subtasks:
        est_in = selector.estimate_tokens(st.get("inputChars", "")) + 200
        cost = None
        if default_cand:
            cost = estimate_subtask_cost(default_cand, est_in, est_out_tokens, now, balance)
        if cost is None:
            unknown += 1
            cost = 0.0
        cost *= safety_factor
        total += cost
        per.append({"id": st["id"], "estInputTokens": est_in,
                    "estCostCny": round(cost, 4),
                    "safetyFactor": safety_factor})
    expected_rounds = max(1, min(int(max_rounds if max_rounds is not None else 3), 2))
    total = total * expected_rounds * overhead_factor
    return {"status": "report",  # v15.4：恒为报告态，不再拒派
            "estimatedCny": round(total, 4),
            "balanceCoverage": _coverage(total, balance or selector.load_balance_snapshot()),
            "unknownCostCount": unknown,
            "safetyFactor": safety_factor, "maxRounds": max_rounds,
            "expectedRounds": expected_rounds,
            "overheadFactor": overhead_factor,
            "note": "v15.4 余额感知报告：预算只做选择参考，不做用量控制",
            "perSubtask": per}
