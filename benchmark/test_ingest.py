"""H2 ingester 测试：benchmark/scores → capabilities.json 的 EMA 合并、_source_run_ids、幂等性。

验收：每条非零维度至少有 1 个 _source_run_ids；重复摄入同一 run_id 结果不抖动。
"""
import json
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from benchmark import capability_ingest  # noqa: E402

FAILED = []

def check(name, cond):
    if cond:
        print(f"  ✓ {name}")
    else:
        print(f"  ✗ {name}")
        FAILED.append(name)

def make_caps():
    return {
        "revision": 0,
        "models": {
            "m1__off": {
                "baseModel": "m1", "thinking": "off", "provider": "p1",
                "capabilities": {
                    "reasoning": {"score": 8.0, "samples": 10, "freshness": 1.0,
                                  "interpolated": False, "_source_run_ids": []},
                    "code": {"score": None, "samples": 0, "freshness": 1.0,
                             "interpolated": False, "_source_run_ids": []},
                },
            },
            "m2__off": {  # 档案中不存在的候选不应被凭空摄入
                "baseModel": "m2", "thinking": "off", "provider": "p1",
                "capabilities": {"reasoning": {"score": 7.0, "samples": 10}},
            },
        },
    }

def make_cases():
    return {
        "m1__off": {
            "reasoning": [("R1", 9.0, 100), ("R2", 9.5, 101)],
            "code": [("C1", 6.0, 100)],
        },
        "ghost__off": {  # 档案中没有 → 应被忽略
            "reasoning": [("R1", 10.0, 100)],
        },
    }

def test_plan_ingest():
    print("[ingest 计划]")
    caps = make_caps()
    new_caps, summary = capability_ingest.plan_ingest(caps, make_cases())
    check("摄入 3 个新 case", summary["ingestedCases"] == 3)
    check("changedDims=2（reasoning 变化 + code 从 None 填充）",
          summary["changedDims"] >= 2)
    entry = new_caps["models"]["m1__off"]["capabilities"]["reasoning"]
    check("reasoning 有 _source_run_ids", set(entry["_source_run_ids"]) == {"R1", "R2"})
    check("reasoning EMA 更新（8→8.42±0.1）", abs(entry["score"] - 8.42) < 0.1)
    code = new_caps["models"]["m1__off"]["capabilities"]["code"]
    check("code 从 None 填充 6.0", code["score"] == 6.0)
    check("revision 自增 0→1", new_caps["revision"] == 1)
    check("不凭空摄入档案外候选", "ghost__off" not in new_caps["models"])

def test_idempotency():
    print("[ingest 幂等]")
    caps = make_caps()
    new1, s1 = capability_ingest.plan_ingest(caps, make_cases())
    new2, s2 = capability_ingest.plan_ingest(new1, make_cases())
    check("二次摄入 0 新增", s2["ingestedCases"] == 0)
    check("二次摄入分数不抖动", json.dumps(new1["models"], sort_keys=True) ==
          json.dumps(new2["models"], sort_keys=True))
    check("二次摄入 revision 不变", new2["revision"] == new1["revision"])
    # 增量摄入：新 case 出现时才再变
    cases2 = make_cases()
    cases2["m1__off"]["reasoning"].append(("R3", 8.0, 200))
    new3, s3 = capability_ingest.plan_ingest(new2, cases2)
    check("新 case 到达→只摄入 1 例", s3["ingestedCases"] == 1)
    check("R3 进入 _source_run_ids", "R3" in
          new3["models"]["m1__off"]["capabilities"]["reasoning"]["_source_run_ids"])
    check("新 case 到达→revision 再自增", new3["revision"] == 2)

def test_ingest_disk(tmp_path: Path):
    print("[ingest 落盘（临时目录）]")
    caps_path = tmp_path / "capabilities.json"
    caps_path.write_text(json.dumps(make_caps()), encoding="utf-8")
    scores = tmp_path / "scores" / "m1__off"
    scores.mkdir(parents=True)
    (scores / "R1.json").write_text(json.dumps(
        {"cand_id": "m1__off", "model": "m1", "thinking": "off",
         "case_id": "R1", "dimension": "reasoning", "score": 9.0, "ts": 100}), encoding="utf-8")
    out1 = capability_ingest.ingest(caps_path=caps_path, scores_root=tmp_path / "scores")
    check("首次落盘 ingested=1", out1["ingestedCases"] == 1)
    out2 = capability_ingest.ingest(caps_path=caps_path, scores_root=tmp_path / "scores")
    check("重复落盘 skipped（幂等）", out2.get("skipped") is True)
    loaded = json.loads(caps_path.read_text(encoding="utf-8"))
    check("落盘后 revision=1", loaded["revision"] == 1)
    check("落盘后档案含 _source_run_ids", "R1" in
          loaded["models"]["m1__off"]["capabilities"]["reasoning"]["_source_run_ids"])

def main():
    test_plan_ingest()
    test_idempotency()
    with tempfile.TemporaryDirectory() as td:
        test_ingest_disk(Path(td))
    print()
    if FAILED:
        print(f"❌ {len(FAILED)} 项失败: {FAILED}")
        sys.exit(1)
    print("✅ 全部通过")

if __name__ == "__main__":
    main()
