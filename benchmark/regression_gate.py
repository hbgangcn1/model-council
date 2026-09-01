"""L3：基准回归门禁（CI 红灯）。

按 (cand_id, dimension) 维护基线（均值±标准差）。门禁比较「最新一批用例」与该维度历史基线：
任何维度最新批均值 < 基线均值 − 1×σ → 退出码 1（CI 红灯阻止合并），并输出回归清单。

用法：
  python benchmark/regression_gate.py --update-baseline   # 用当前全部数据重算基线（baseline 不存在时自动做）
  python benchmark/regression_gate.py                    # 门禁检查（退出码 0=通过 / 1=回归）
"""
import json
import math
import sys
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent  # council/
SCORES_ROOT = BASE / "benchmark" / "scores"
BASELINE_FILE = BASE / "benchmark" / "regression-baseline.json"
LATEST_BATCH_SIZE = 4   # 每个维度按 ts 取最新 N 例作为「最新一批」


def _load_all_scores(scores_root: Path = SCORES_ROOT) -> dict:
    """→ {cid: {dim: [(ts, case_id, score)]}}，按 ts 升序。"""
    out = {}
    if not scores_root.exists():
        return out
    for cid_dir in sorted(p for p in scores_root.iterdir() if p.is_dir()):
        dims = {}
        for f in sorted(cid_dir.glob("*.json")):
            try:
                rec = json.loads(f.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, OSError):
                continue
            if not isinstance(rec, dict) or rec.get("cand_id") != cid_dir.name:
                continue
            dim = rec.get("dimension")
            score = rec.get("score")
            if dim and isinstance(score, (int, float)):
                dims.setdefault(dim, []).append((rec.get("ts", 0), f.stem, float(score)))
        for dim in dims:
            dims[dim].sort(key=lambda t: t[0])
        if dims:
            out[cid_dir.name] = dims
    return out


def _mean_std(vals):
    n = len(vals)
    if n == 0:
        return None, None, 0
    mean = sum(vals) / n
    var = sum((v - mean) ** 2 for v in vals) / n
    return mean, math.sqrt(var), n


def build_baseline(all_scores: dict = None) -> dict:
    """全量历史 → 基线 {cid: {dim: {mean, std, n}}}。"""
    all_scores = all_scores if all_scores is not None else _load_all_scores()
    baseline = {}
    for cid, dims in all_scores.items():
        bd = {}
        for dim, rows in dims.items():
            mean, std, n = _mean_std([s for _, _, s in rows])
            if n == 0:
                continue
            bd[dim] = {"mean": round(mean, 3), "std": round(std, 3), "n": n}
        if bd:
            baseline[cid] = bd
    return baseline


def check_regressions(all_scores: dict = None, baseline: dict = None,
                      batch_size: int = LATEST_BATCH_SIZE) -> dict:
    """最新一批 vs 基线：返回 {regressions: [{cid, dim, latestMean, baselineMean, std, threshold}]}。"""
    all_scores = all_scores if all_scores is not None else _load_all_scores()
    baseline = baseline if baseline is not None else build_baseline(all_scores)
    regressions = []
    for cid, dims in all_scores.items():
        base_dims = baseline.get(cid, {})
        for dim, rows in dims.items():
            b = base_dims.get(dim)
            if not b:
                continue
            latest = rows[-batch_size:]
            # 基线样本不足 2 例不做门禁（无法可靠估 σ）
            if b["n"] < 2 or not latest:
                continue
            latest_mean = sum(s for _, _, s in latest) / len(latest)
            threshold = b["mean"] - b["std"]
            if latest_mean < threshold:
                regressions.append({
                    "cid": cid, "dim": dim,
                    "latestMean": round(latest_mean, 3),
                    "baselineMean": b["mean"], "std": b["std"],
                    "threshold": round(threshold, 3),
                    "delta": round(latest_mean - b["mean"], 3),
                })
    return {"regressions": regressions,
            "checked": sum(len(d) for d in all_scores.values()),
            "baselineEntries": sum(len(d) for d in baseline.values())}


def update_baseline_file(baseline_file: Path = BASELINE_FILE,
                         scores_root: Path = SCORES_ROOT) -> dict:
    baseline = build_baseline(_load_all_scores(scores_root))
    tmp = baseline_file.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(baseline, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(baseline_file)
    return baseline


def main():
    args = set(sys.argv[1:])
    if "--update-baseline" in args or not BASELINE_FILE.exists():
        print("== 更新基线 ==")
        baseline = update_baseline_file()
        print(f"基线已写入 {BASELINE_FILE}（{sum(len(d) for d in baseline.values())} 个维度条目）")
    baseline = json.loads(BASELINE_FILE.read_text(encoding="utf-8"))
    report = check_regressions(baseline=baseline)
    print(json.dumps(report, ensure_ascii=False, indent=2))
    if report["regressions"]:
        print(f"❌ 回归门禁未通过：{len(report['regressions'])} 个维度低于历史均值 1σ")
        sys.exit(1)
    print(f"✅ 回归门禁通过：{report['checked']} 个维度样例均未跌破基线 1σ")
    sys.exit(0)


if __name__ == "__main__":
    main()
