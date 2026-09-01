"""judge_drift 离线逻辑测试（评审报告 M 项）：不调 API，只测纯函数与阈值判定。
"""
import json
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from orchestrator import judge_drift  # noqa: E402

FAILED = []

def check(name, cond):
    if cond:
        print(f"  ✓ {name}")
    else:
        print(f"  ✗ {name}")
        FAILED.append(name)

def test_extract_score():
    print("[judge 打分解析]")
    check("标准 JSON", judge_drift._extract_score('{"score": 8.5, "rationale": "ok"}') == 8.5)
    check("带杂音包裹", judge_drift._extract_score('前缀 {"score": 7.0} 后缀') == 7.0)
    check("分数越界收敛由调用方做（解析不越界）", judge_drift._extract_score('{"score": 99}') == 99.0)
    check("不可解析→None", judge_drift._extract_score("no json here") is None)

def test_compute_drift():
    print("[漂移阈值判定]")
    check("零漂移不告警", judge_drift.compute_drift(8.0, 8.0, 1.0) == {"drift": 0.0, "alerted": False})
    check("超阈值正漂移告警", judge_drift.compute_drift(9.5, 8.0, 1.0) == {"drift": 1.5, "alerted": True})
    check("超阈值负漂移告警", judge_drift.compute_drift(6.0, 8.0, 1.0) == {"drift": -2.0, "alerted": True})
    check("阈值内不告警", judge_drift.compute_drift(8.9, 8.0, 1.0) == {"drift": 0.9, "alerted": False})
    check("均值缺失不告警", judge_drift.compute_drift(None, 8.0, 1.0) == {"drift": None, "alerted": False})

def test_golden_set_shape():
    print("[金标集结构]")
    golden = json.loads(judge_drift.GOLDEN.read_text(encoding="utf-8"))
    items = golden.get("items") or []
    check("金标集 ≥8 题", len(items) >= 8)
    check("每题字段齐全（task/answer/rubric）",
          all(i.get("task") and i.get("answer") and i.get("rubric") for i in items))
    check("id 唯一", len({i["id"] for i in items}) == len(items))

def test_init_baseline_min_items():
    print("[基线最少有效题数]")
    items = [{"id": f"G{i}"} for i in range(4)]
    scores = [{"id": "G0", "ok": True, "score": 8.0},
              {"id": "G1", "ok": True, "score": 8.5},
              {"id": "G2", "ok": False, "error": "timeout"},
              {"id": "G3", "ok": False, "error": "timeout"}]
    with tempfile.TemporaryDirectory() as td:
        judge_drift.BASELINE = Path(td) / "judge-baseline.json"
        try:
            judge_drift.init_baseline(items, scores)
            check("有效题 <3 应拒绝建基线（不应到达）", False)
        except RuntimeError as e:
            check("有效题不足 → RuntimeError", "不足" in str(e))

def main():
    test_extract_score()
    test_compute_drift()
    test_golden_set_shape()
    test_init_baseline_min_items()
    print()
    if FAILED:
        print(f"❌ {len(FAILED)} 项失败: {FAILED}")
        sys.exit(1)
    print("✅ 全部通过")

if __name__ == "__main__":
    main()
