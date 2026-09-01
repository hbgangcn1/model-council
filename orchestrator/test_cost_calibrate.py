"""H3 成本对账测试：est vs actual drift、7 日窗口、落盘。
"""
import json
import os
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from orchestrator import cost_calibrate  # noqa: E402

FAILED = []

def check(name, cond):
    if cond:
        print(f"  ✓ {name}")
    else:
        print(f"  ✗ {name}")
        FAILED.append(name)

def test_calibrate(tmp_path: Path):
    print("[cost_calibrate 对账]")
    runs = tmp_path / "runs"
    new_run = runs / "2026-08-23_10-00-00"
    new_run.mkdir(parents=True)
    (new_run / "cost.jsonl").write_text("\n".join([
        json.dumps({"role": "exec", "estCostCny": 0.05, "actualCostCny": 0.052}),
        json.dumps({"role": "verifier", "estCostCny": 0.03, "actualCostCny": None}),
        json.dumps({"garbage": True}),  # 损坏行跳过
    ]), encoding="utf-8")
    # 8 天前的旧 run：超出 7 日窗口，不应计入
    old_run = runs / "2026-08-14_10-00-00"
    old_run.mkdir(parents=True)
    (old_run / "cost.jsonl").write_text(
        json.dumps({"role": "exec", "estCostCny": 9.99, "actualCostCny": 9.99}), encoding="utf-8")
    old_ts = time.time() - 8 * 86400
    os.utime(old_run, (old_ts, old_ts))
    out = cost_calibrate.calibrate(runs_dir=runs, out_path=tmp_path / "cost-drift.json")
    check("仅统计 7 日内 run（1 个）", out["runs"] == 1)
    check("est 汇总 0.08", abs(out["estCny"] - 0.08) < 1e-9)
    check("actual 汇总 0.052（缺 usage 行不计 actual）", abs(out["actualCny"] - 0.052) < 1e-9)
    check("drift = (0.052-0.08)/0.08 = -35.0%", out["driftPct"] == -35.0)
    check("pricedRows=1", out["pricedRows"] == 1)
    check("落盘 cost-drift.json", (tmp_path / "cost-drift.json").exists())

def test_drift_null_without_usage(tmp_path: Path):
    print("[无 usage 数据时 drift 为空]")
    runs = tmp_path / "runs2"
    r = runs / "2026-08-23_11-00-00"
    r.mkdir(parents=True)
    (r / "cost.jsonl").write_text(
        json.dumps({"role": "exec", "estCostCny": 0.05, "actualCostCny": None}), encoding="utf-8")
    out = cost_calibrate.calibrate(runs_dir=runs, out_path=tmp_path / "drift2.json")
    check("全估算数据 → driftPct=None（不造假）", out["driftPct"] is None)

def main():
    with tempfile.TemporaryDirectory() as td:
        test_calibrate(Path(td))
        test_drift_null_without_usage(Path(td))
    print()
    if FAILED:
        print(f"❌ {len(FAILED)} 项失败: {FAILED}")
        sys.exit(1)
    print("✅ 全部通过")

if __name__ == "__main__":
    main()
