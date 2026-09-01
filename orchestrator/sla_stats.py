"""预算 SLA 统计（评审报告 M 项）：从 runs/*/result.json 计算各档 p50/p95 延迟与成本，
与 council-params.json 的 sla.* 目标对比，落盘 sla-report.json（供 UI/council_status/月度复盘）。

指标口径：
- wallS：run 目录内最早文件 mtime → result.json mtime（进程墙钟近似）；
- costCny：result.json 的 cost_so_far（全程累计成本，decompose→synthesize）；
- 每档不足 2 个样本时只给实测值不给 p95（避免小样本伪精度）。

用法：python orchestrator/sla_stats.py
"""
import json
import sys
from pathlib import Path

try:
    from .config_loader import now_shanghai
    from . import params as params_mod
except ImportError:
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    from config_loader import now_shanghai
    import params as params_mod  # noqa: E402

BASE = Path(__file__).resolve().parent.parent  # council/
RUNS = BASE / "runs"
OUT = BASE / "sla-report.json"


def _percentile(sorted_vals: list, p: float):
    if not sorted_vals:
        return None
    k = (len(sorted_vals) - 1) * p
    lo = int(k)
    hi = min(lo + 1, len(sorted_vals) - 1)
    return round(sorted_vals[lo] + (sorted_vals[hi] - sorted_vals[lo]) * (k - lo), 1)


def _wall_seconds(run_dir: Path) -> float:
    try:
        files = [f for f in run_dir.rglob("*") if f.is_file()]
        if not files:
            return None
        start = min(f.stat().st_mtime for f in files)
        end = max(f.stat().st_mtime for f in files)
        return round(end - start, 1)
    except OSError:
        return None


def collect() -> dict:
    """→ {tier: [{wallS, costCny, run, status}]}（只统计写有 result.json 的 run）。"""
    by_tier = {}
    if not RUNS.exists():
        return by_tier
    for run_dir in sorted(p for p in RUNS.iterdir() if p.is_dir()):
        result_file = run_dir / "result.json"
        if not result_file.exists():
            continue
        try:
            res = json.loads(result_file.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            continue
        if res.get("status") in ("ok_dry", "budget_precheck_over", "decompose_failed"):
            continue  # 未真正执行完收敛循环的 run 不进 SLA 统计
        tier = res.get("tier") or "standard"
        by_tier.setdefault(tier, []).append({
            "run": run_dir.name,
            "wallS": _wall_seconds(run_dir),
            "costCny": res.get("cost_so_far"),
            "rounds": res.get("rounds"),
            "status": res.get("status"),
        })
    return by_tier


def report() -> dict:
    p = params_mod.load()
    sla_targets = p.get("sla", params_mod.DEFAULTS["sla"])
    by_tier = collect()
    per_tier = {}
    for tier in sorted(sla_targets.keys()):
        if tier.startswith("_"):   # v15.5b：跳过 params 注释键（_comment）
            continue
        rows = by_tier.get(tier, [])
        walls = sorted(r["wallS"] for r in rows if r.get("wallS") is not None)
        costs = sorted(r["costCny"] for r in rows if r.get("costCny") is not None)
        target = sla_targets[tier]
        entry = {
            "samples": len(rows),
            "target": target,
            "observed": {
                "wallP50S": _percentile(walls, 0.5),
                "wallP95S": _percentile(walls, 0.95) if len(walls) >= 2 else None,
                "costP50Cny": _percentile(costs, 0.5),
                "costP95Cny": _percentile(costs, 0.95) if len(costs) >= 2 else None,
                "maxCostCny": round(max(costs), 4) if costs else None,
            },
            "violations": [],
        }
        obs = entry["observed"]
        if obs["wallP95S"] is not None and obs["wallP95S"] > target.get("wallP95S", 10**9):
            entry["violations"].append(f"wallP95 {obs['wallP95S']}s > 目标 {target.get('wallP95S')}s")
        # v15.4：成本目标降为纯观察——costCapCny 为空/缺失时不判违规（继续统计 maxCost 供复盘）
        cap = target.get("costCapCny")
        if cap and obs["maxCostCny"] is not None and obs["maxCostCny"] > cap:
            entry["violations"].append(
                f"单 run 最高成本 ¥{obs['maxCostCny']} > 上限 ¥{cap}")
        # v15.5c（元评审 2026-08-26_21-50-46）：每档附 run 明细（按 wall 降序），
        # 让 P95 超标可归因——实测 fast P95 由历史异常 run（8/25 1019.9s，旧 bug 时代）
        # 拖高，非「收敛早停失效」，明细让此类误判不再发生。
        entry["runs"] = sorted(rows, key=lambda r: -(r.get("wallS") or 0))[:10]
        per_tier[tier] = entry
    out = {
        "generatedAt": now_shanghai().isoformat(),
        "note": ("SLA 目标见 DESIGN-v14.md §4.1（观察性承诺，非硬约束）；"
                 "v15.5 起 run 墙钟为动态预算（wallBudget.*，上限 1680s=宿主 1920s−240s 余量），"
                 "墙钟不再按 1800s 固定值判；月度复盘对比 observed 与 target，"
                 "偏差持续超限应校准 params.sla 或档位参数。"),
        "tiers": per_tier,
    }
    tmp = OUT.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(out, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(OUT)
    return out


if __name__ == "__main__":
    print(json.dumps(report(), ensure_ascii=False, indent=2))
