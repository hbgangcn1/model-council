"""P1-3：pairwise/ELO 横向比较（元评审 2026-08-24）。

原理：runtime-feedback.jsonl 每行带 (run_id, case_id, model, verifierScore, scoredBy)。
同一 run 内先按 verifier 分组做 z-score 校准（消除松紧差，与 update_capabilities 同口径），
再按 baseModel 两两比较（z 高者胜，|Δz| < draw_eps 判平），Elo K=32 增量更新。
低 Elo 模型在 selector 中被软降权（eloPenaltyScale），而非剔除。

用法：
  python orchestrator/pairwise.py            # 计算并落盘 elo.json
  python orchestrator/pairwise.py --dry      # 只算不落盘
"""
import json
import math
import os
import sys
from pathlib import Path

try:
    from .config_loader import now_shanghai
    from . import params as params_mod
except ImportError:
    from config_loader import now_shanghai  # type: ignore
    import params as params_mod  # type: ignore

BASE = Path(__file__).resolve().parent.parent  # council/
FEEDBACK = BASE / "evals" / "runtime-feedback.jsonl"
OUT = Path(os.environ.get("COUNCIL_ELO_FILE", str(BASE / "elo.json")))

DRAW_EPS = 0.05      # |Δz| 小于该值判平
K = 32               # Elo K 因子
ELO0 = 1500


def _z_by_run(rows):
    """按 (run_id) 分组 z-score。与 update_capabilities._group_zscore 同口径。"""
    by_run = {}
    for r in rows:
        by_run.setdefault(r.get("run_id"), []).append(r)
    for run_id, group in by_run.items():
        scores = [g["verifierScore"] for g in group if g.get("verifierScore") is not None]
        if len(scores) < 2:
            for g in group:
                g["z"] = 0.0
            continue
        mean = sum(scores) / len(scores)
        std = math.sqrt(sum((s - mean) ** 2 for s in scores) / len(scores)) or 1.0
        for g in group:
            g["z"] = ((g.get("verifierScore") or mean) - mean) / std
    return rows


def _load_rows():
    if not FEEDBACK.exists():
        return []
    rows = []
    for line in FEEDBACK.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            rows.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return [r for r in rows if r.get("success") and not r.get("hardGateHit")
            and not r.get("reworkTriggered") and r.get("verifierScore") is not None
            and r.get("model")]


def _elo_delta(ra, rb, outcome):
    """outcome: 1=a 胜, 0=平, -1=a 负。"""
    ea = 1.0 / (1.0 + 10 ** ((rb - ra) / 400.0))
    return K * (outcome - ea)


def compute(rows=None, elo0=ELO0, k=K, draw_eps=DRAW_EPS):
    """→ (ratings, samples, pair_counts)。纯函数，可离线测试。"""
    rows = _load_rows() if rows is None else rows
    rows = _z_by_run([dict(r) for r in rows])
    ratings = {}
    samples = {}
    pairs = 0
    by_run = {}
    for r in rows:
        by_run.setdefault(r.get("run_id"), []).append(r)
    for run_id, group in by_run.items():
        for i in range(len(group)):
            for j in range(i + 1, len(group)):
                a, b = group[i], group[j]
                ma, mb = a.get("model"), b.get("model")
                if not ma or not mb or ma == mb:
                    continue
                ratings.setdefault(ma, elo0)
                ratings.setdefault(mb, elo0)
                dz = a["z"] - b["z"]
                outcome = 1 if dz > draw_eps else (-1 if dz < -draw_eps else 0)
                ra, rb = ratings[ma], ratings[mb]
                ratings[ma] = ra + _elo_delta(ra, rb, outcome)
                ratings[mb] = rb + _elo_delta(rb, ra, -outcome)
                samples[ma] = samples.get(ma, 0) + 1
                samples[mb] = samples.get(mb, 0) + 1
                pairs += 1
    return ratings, samples, pairs


def update(dry=False):
    rows = _load_rows()
    ratings, samples, pairs = compute(rows)
    source_run_ids = sorted({r.get("run_id") for r in rows if r.get("run_id")})
    out = {"updatedAt": now_shanghai().isoformat(), "ratings": ratings,
           "samples": samples, "pairComparisons": pairs,
           "sourceRunIds": source_run_ids,
           "note": "Elo 来自 runtime-feedback 同 run 内 z 校准后的两两比较（P1-3 横向比较）"}
    if not dry:
        tmp = OUT.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(out, ensure_ascii=False, indent=2), encoding="utf-8")
        tmp.replace(OUT)
    return out


if __name__ == "__main__":
    dry = "--dry" in sys.argv
    print(json.dumps(update(dry=dry), ensure_ascii=False, indent=2))
