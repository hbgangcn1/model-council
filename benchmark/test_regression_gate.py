"""L3 回归门禁测试：基线构建、最新批跌破 mean-1σ 检出、正常数据通过。
"""
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from benchmark import regression_gate  # noqa: E402

FAILED = []

def check(name, cond):
    if cond:
        print(f"  ✓ {name}")
    else:
        print(f"  ✗ {name}")
        FAILED.append(name)

def make_scores(good: bool):
    """cand1/reasoning 历史 10 例 7.5/8.5 交替（均值 8.0、σ=0.5）；
    最新批 good=False 时骤降到 5.0（跌破 mean-1σ=7.5）。"""
    rows = [(100 + i, f"R{i}", 7.5 if i % 2 == 0 else 8.5) for i in range(10)]
    latest_ts = 1000
    if good:
        rows += [(latest_ts + i, f"L{i}", 7.8) for i in range(4)]
    else:
        rows += [(latest_ts + i, f"L{i}", 5.0) for i in range(4)]
    return {"cand1__off": {"reasoning": rows}}

def test_gate():
    print("[回归门禁：正常数据通过]")
    baseline = regression_gate.build_baseline(make_scores(good=True))
    report = regression_gate.check_regressions(make_scores(good=True), baseline)
    check("无回归时 regressions 为空", report["regressions"] == [])
    check("checked 计数正确", report["checked"] == 1)

def test_gate_catches_regression():
    print("[回归门禁：跌破 1σ 检出]")
    baseline = regression_gate.build_baseline(make_scores(good=True))
    report = regression_gate.check_regressions(make_scores(good=False), baseline)
    check("检出回归", len(report["regressions"]) == 1)
    r = report["regressions"][0]
    check("回归维度/cid 正确", r["cid"] == "cand1__off" and r["dim"] == "reasoning")
    check("最新批均值 5.0", r["latestMean"] == 5.0)
    check("阈值 = 基线均值 − 1σ（7.5 > 5.0 即检出）", r["threshold"] > r["latestMean"])

def test_baseline_file(tmp_path: Path):
    print("[基线文件落盘]")
    bf = tmp_path / "regression-baseline.json"
    # 直接构建（不依赖全局路径）
    baseline = regression_gate.build_baseline(make_scores(good=True))
    bf.write_text(str(baseline).replace("'", '"'), encoding="utf-8")
    check("基线含 cand1__off/reasoning",
          "reasoning" in baseline.get("cand1__off", {}))

def main():
    test_gate()
    test_gate_catches_regression()
    with tempfile.TemporaryDirectory() as td:
        test_baseline_file(Path(td))
    print()
    if FAILED:
        print(f"❌ {len(FAILED)} 项失败: {FAILED}")
        sys.exit(1)
    print("✅ 全部通过")

if __name__ == "__main__":
    main()
