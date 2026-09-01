"""双验证者尺度校准（2-baseModel 常态模式）：两家 verifier 打分 z-score 归一化后聚合。"""
import math

def zscore_normalize(rows: list) -> list:
    """rows: [{subtask_id, verifier, score}] → 每 verifier 内 z-score 归一化。
    返回 rows 附带 z 字段。"""
    by_v = {}
    for r in rows:
        by_v.setdefault(r["verifier"], []).append(r["score"])
    stats = {}
    for v, scores in by_v.items():
        n = len(scores)
        mean = sum(scores) / n
        std = math.sqrt(sum((x - mean) ** 2 for x in scores) / n) if n > 1 else 1.0
        stats[v] = (mean, std if std > 0 else 1.0)
    for r in rows:
        mean, std = stats[r["verifier"]]
        r["z"] = (r["score"] - mean) / std
    return rows

def aggregate(rows: list) -> dict:
    """聚合：跨 verifier z 对齐（消除松紧差），再加回公共水平（z × 公共std + 公共均值），
    映射回 0-10。S_r 保留真实水平——修复「S_r 恒 5.0」bug（z 均值恒 0 导致的）。
    空 rows（本轮无任何有效 verdict）→ 返回 S_r=0 而不崩，交由 terminator 决策。"""
    if not rows:
        return {"subtask_scores": {}, "S_r": 0.0}
    rows = zscore_normalize(rows)
    all_scores = [r["score"] for r in rows]
    grand_mean = sum(all_scores) / len(all_scores)
    grand_std = (sum((x - grand_mean) ** 2 for x in all_scores) / len(all_scores)) ** 0.5
    grand_std = grand_std if grand_std > 0 else 1.0
    per = {}
    for r in rows:
        per.setdefault(r["subtask_id"], []).append(r["z"])
    mapped = {}
    for sid, zs in per.items():
        z_avg = sum(zs) / len(zs)
        aligned = grand_mean + z_avg * grand_std
        mapped[sid] = round(max(0.0, min(10.0, aligned)), 2)
    overall = round(sum(mapped.values()) / len(mapped), 2) if mapped else 0.0
    return {"subtask_scores": mapped, "S_r": overall}
