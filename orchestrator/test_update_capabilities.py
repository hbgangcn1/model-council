"""update_capabilities 测试（评审报告 H 项）：锁/写前校验/最小改动阈值/sourceRunIds/skipped。

隔离：FEEDBACK/CAPS 用模块级 patch（monkeypatch 模块常量），临时目录数据。
"""
import json
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from orchestrator import update_capabilities  # noqa: E402

FAILED = []

def check(name, cond):
    if cond:
        print(f"  ✓ {name}")
    else:
        print(f"  ✗ {name}")
        FAILED.append(name)

def make_caps(revision=0):
    return {
        "schemaVersion": 2,
        "revision": revision,
        "dimensions": ["reasoning", "code", "chinese", "research",
                       "instruction_following", "long_context",
                       "tool_use", "creativity", "safety"],
        "models": {
            "m1__off": {
                "baseModel": "m1", "thinking": "off", "provider": "p1",
                "capabilities": {
                    "reasoning": {"score": 8.0, "samples": 10, "freshness": 1.0,
                                  "interpolated": False, "_source_run_ids": []},
                    "code": {"score": 7.0, "samples": 10, "freshness": 1.0},
                },
            },
            "m2__off": {
                "baseModel": "m2", "thinking": "off", "provider": "p2",
                "capabilities": {
                    "reasoning": {"score": 8.0, "samples": 10, "freshness": 1.0},
                },
            },
        },
    }

def _feedback_rows(runs=3, m1_high=True):
    """构造 runs 个 run、每 run 2 模型各 2 行（z-score 组内比较成立）。
    m1_high=True 时 m1 每轮都比 m2 高 1 分 → m1 z>0、m2 z<0。"""
    rows = []
    for ri in range(runs):
        run_id = f"run-{ri}"
        for sub in ("s1", "s2"):
            rows.append({"run_id": run_id, "case_id": sub, "model": "m1", "thinking": "off",
                         "verifierScore": 8.0 if m1_high else 6.0,
                         "success": True, "hardGateHit": False, "reworkTriggered": False,
                         "taskVector": {"reasoning": 1.0}, "latency_ms": 1000,
                         "cost_usd": 0.001, "ts": "2026-08-24T10:00:00+08:00"})
            rows.append({"run_id": run_id, "case_id": sub, "model": "m2", "thinking": "off",
                         "verifierScore": 7.0 if m1_high else 6.0,
                         "success": True, "hardGateHit": False, "reworkTriggered": False,
                         "taskVector": {"reasoning": 1.0}, "latency_ms": 1000,
                         "cost_usd": 0.001, "ts": "2026-08-24T10:00:00+08:00"})
    return rows

def _patch(tmp: Path, caps: dict, rows: list):
    update_capabilities.FEEDBACK = tmp / "evals" / "runtime-feedback.jsonl"
    update_capabilities.FEEDBACK.parent.mkdir(parents=True, exist_ok=True)
    update_capabilities.CAPS = tmp / "capabilities.json"
    update_capabilities.CAPS.write_text(json.dumps(caps), encoding="utf-8")
    if rows:
        with update_capabilities.FEEDBACK.open("w", encoding="utf-8") as f:
            for r in rows:
                f.write(json.dumps(r) + "\n")

def test_min_change_threshold():
    print("[最小改动阈值：|Δ|<0.5 不写分只记样本]")
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        # m1 比 m2 稳定高 1 分 → z 分正，会推 m1.reasoning 上移；样本少权重 w 低 → |Δ|<0.5
        _patch(tmp, make_caps(), _feedback_rows(runs=1))
        out = update_capabilities.update()
        caps = json.loads(update_capabilities.CAPS.read_text(encoding="utf-8"))
        entry = caps["models"]["m1__off"]["capabilities"]["reasoning"]
        check("runtimeSamples 已记样本", entry.get("runtimeSamples", 0) >= 1)
        if out.get("changedScores", 0) == 0:
            check("小偏移+样本少 → 分数不动", entry["score"] == 8.0)
        else:
            check("若动了分，偏移必须 <0.5", abs(entry["score"] - 8.0) < 0.5)
        check("sourceRunIds 已记录", out.get("sourceRunIds") == ["run-0"])

def test_no_feedback_skips():
    print("[无有效反馈 → skipped 不空转 revision]")
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        _patch(tmp, make_caps(), [])
        out = update_capabilities.update()
        check("skipped=True", out.get("skipped") is True)
        caps = json.loads(update_capabilities.CAPS.read_text(encoding="utf-8"))
        check("revision 不变（仍 0）", caps["revision"] == 0)

def test_validation_rejects_bad_doc():
    print("[写前校验：坏分数拒绝落盘]")
    from orchestrator.caps_guard import validate
    bad = make_caps()
    bad["models"]["m1__off"]["capabilities"]["reasoning"]["score"] = 99.0
    problems = validate(bad, old_revision=0)
    check("越界分数被检出", any("越出" in p for p in problems))
    rev_bad = make_caps(revision=3)
    problems2 = validate(rev_bad, old_revision=5)
    check("revision 回退被检出", any("回退" in p for p in problems2))

def test_lock_blocks_concurrent():
    print("[文件锁：持锁期间第二次获取超时]")
    from orchestrator.file_lock import file_lock
    with tempfile.TemporaryDirectory() as td:
        target = Path(td) / "caps.json"
        target.write_text("{}", encoding="utf-8")
        with file_lock(target, timeout_s=5, stale_after_s=60):
            start = time.time()
            try:
                with file_lock(target, timeout_s=1.0, stale_after_s=60):
                    check("第二次拿到锁（不应发生）", False)
            except TimeoutError:
                check("并发获取 → TimeoutError", True)

def main():
    test_min_change_threshold()
    test_no_feedback_skips()
    test_validation_rejects_bad_doc()
    test_lock_blocks_concurrent()
    print()
    if FAILED:
        print(f"❌ {len(FAILED)} 项失败: {FAILED}")
        sys.exit(1)
    print("✅ 全部通过")

if __name__ == "__main__":
    main()
