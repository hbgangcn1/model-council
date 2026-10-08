"""Judge-aperture 离线测试（fixtures 全在 tmp_path，从不碰 benchmark/scores/）。

覆盖：
1. bench/runner.py 写判分成绩时打 judgeVendorGroup + sameVendorJudge；
   确定性判分（scorer 路径）保持 judge:null 且不加新字段。
2. capability_ingest 按维度加权均值：同厂 judge 主观分权重 0.5；
   build_diff 产出的 pending-ingest-diff.json 含 downWeighted[]。
3. bench/summary.py summarize() 含 judgeAperture{j judgesUsed,
   sameVendorPairs, dateRange}。

全离线：无 API/LLM 调用。pytest 或 python 直接跑均可。
"""
import json
import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))
from benchmark import capability_ingest  # noqa: E402
from bench import config as bench_config  # noqa: E402
from bench import runner as bench_runner  # noqa: E402
from bench import summary as summary_mod  # noqa: E402
from bench import cross_judge as cross_judge_mod  # noqa: E402

FAILED = []


def check(name, cond):
    if cond:
        print(f"  ok {name}")
    else:
        print(f"  FAIL {name}")
        FAILED.append(name)


def _write_score(root: Path, cid: str, case_id: str, rec: dict):
    d = root / cid
    d.mkdir(parents=True, exist_ok=True)
    (d / f"{case_id}.json").write_text(json.dumps(rec), encoding="utf-8")


def test_vendor_group():
    print("[vendor group]")
    check("MiniMax-M3 -> minimax",
          bench_runner._vendor_group("MiniMax-M3") == "minimax")
    check("带 provider 前缀剥离",
          bench_runner._vendor_group("minimax-portal/MiniMax-M3") == "minimax")
    check("deepseek-flash -> deepseek",
          bench_runner._vendor_group("deepseek-flash") == "deepseek")
    check("None -> None", bench_runner._vendor_group(None) is None)


def test_runner_writes_labels(tmp_path: Path):
    print("[runner 写盘 labeling]")
    scores_dir = tmp_path / "scores"
    scores_dir.mkdir()
    real_scores_dir = bench_config.SCORES_DIR
    bench_config.SCORES_DIR = scores_dir
    real_cj = cross_judge_mod.score_case
    real_sc = None
    try:
        from bench import scorer as scorer_mod
        real_sc = scorer_mod.score_objective_with_spec

        def fake_judge(case, text, target):
            return 8.0, "理由充足超过十五字肯定够了", {
                "verdict": "real", "judge": "MiniMax-M3"}

        def fake_obj(case, text, meta):
            return 7.0, "exact match", {"verdict": "real", "judge": None}

        cross_judge_mod.score_case = fake_judge
        scorer_mod.score_objective_with_spec = fake_obj
        real_load_resp = bench_runner.load_resp
        real_load_score = bench_runner.load_score
        bench_runner.load_resp = lambda cid, case_id: {
            "status": "done", "text": "作答文本", "meta": {}}
        bench_runner.load_score = lambda cid, case_id: None
        args = SimpleNamespace(resume=True)
        judged = {"id": "C3", "dimension": "creativity", "prompt": "p"}
        det = {"id": "R1", "dimension": "reasoning", "prompt": "p"}
        bench_runner.score(args, [judged, det],
                           [("MiniMax-M3", "off"), ("deepseek-flash", "off")])

        mm = json.loads((scores_dir / "MiniMax-M3__off" / "C3.json")
                        .read_text(encoding="utf-8"))
        check("判分文件有 judgeVendorGroup",
              mm.get("judgeVendorGroup") == "minimax")
        check("同厂判分 sameVendorJudge=True",
              mm.get("sameVendorJudge") is True)
        ds = json.loads((scores_dir / "deepseek-flash__off" / "C3.json")
                        .read_text(encoding="utf-8"))
        check("跨厂判分 sameVendorJudge=False",
              ds.get("sameVendorJudge") is False
              and ds.get("judgeVendorGroup") == "minimax")
        r1 = json.loads((scores_dir / "MiniMax-M3__off" / "R1.json")
                        .read_text(encoding="utf-8"))
        check("确定性判分 judge 为 null", r1.get("judge") is None)
        check("确定性判分无 judgeVendorGroup",
              "judgeVendorGroup" not in r1)
        check("确定性判分无 sameVendorJudge",
              "sameVendorJudge" not in r1)
    finally:
        bench_config.SCORES_DIR = real_scores_dir
        bench_runner.load_resp = real_load_resp
        bench_runner.load_score = real_load_score
        cross_judge_mod.score_case = real_cj
        if real_sc is not None:
            scorer_mod.score_objective_with_spec = real_sc


def _caps():
    return {
        "revision": 0,
        "models": {
            "MiniMax-M3__off": {
                "baseModel": "MiniMax-M3", "thinking": "off",
                "provider": "minimax",
                "capabilities": {
                    "creativity": {"score": 8.0, "samples": 10,
                                  "freshness": 1.0, "interpolated": False,
                                  "_source_run_ids": []},
                },
            },
        },
    }


def test_ingest_downweight(tmp_path: Path):
    print("[ingest 降权]")
    root = tmp_path / "scores"
    _write_score(root, "MiniMax-M3__off", "C3", {
        "cand_id": "MiniMax-M3__off", "case_id": "C3",
        "dimension": "creativity", "score": 10.0, "ts": 101,
        "judge": "MiniMax-M3", "judgeVendorGroup": "minimax",
        "sameVendorJudge": True})
    _write_score(root, "MiniMax-M3__off", "CN1", {
        "cand_id": "MiniMax-M3__off", "case_id": "CN1",
        "dimension": "creativity", "score": 6.0, "ts": 102,
        "judge": "deepseek-flash", "judgeVendorGroup": "deepseek",
        "sameVendorJudge": False})
    cases = capability_ingest.collect_cases(root)
    rows = cases["MiniMax-M3__off"]["creativity"]
    check("collect 保留同厂标记",
          any(len(r) > 3 and r[3] is True for r in rows))
    new_caps, summary = capability_ingest.plan_ingest(_caps(), cases)
    # 加权均值 (10*0.5 + 6*1) / 1.5 = 7.333；EMA: 8*0.7 + 7.333*0.3 = 7.8
    got = new_caps["models"]["MiniMax-M3__off"]["capabilities"]["creativity"]["score"]
    check(f"同厂 0.5 加权均值 EMA≈7.8（得 {got}）", abs(got - 7.8) < 0.01)
    check("summary 含 downWeighted",
          len(summary.get("downWeighted") or []) == 1
          and summary["downWeighted"][0]["caseId"] == "C3")

    caps_path = tmp_path / "capabilities.json"
    caps_path.write_text(json.dumps(_caps()), encoding="utf-8")
    out_path = tmp_path / "pending-ingest-diff.json"
    diff = capability_ingest.build_diff(caps_path=caps_path,
                                        scores_root=root, out_path=out_path)
    check("diff 落盘含 downWeighted[]",
          len(diff.get("downWeighted") or []) == 1)
    check("downWeighted 权重 0.5",
          (diff["downWeighted"] or [{}])[0].get("weight") == 0.5)
    print(f"  downWeighted={json.dumps(diff['downWeighted'], ensure_ascii=False)}")


def test_summary_aperture(tmp_path: Path):
    print("[summary judgeAperture]")
    root = tmp_path / "scores"
    _write_score(root, "m__off", "C3", {
        "cand_id": "m__off", "case_id": "C3", "dimension": "creativity",
        "score": 8.0, "ts": 100, "verdict": "real",
        "judge": "MiniMax-M3", "judgeVendorGroup": "minimax",
        "sameVendorJudge": True})
    _write_score(root, "m__off", "CN1", {
        "cand_id": "m__off", "case_id": "CN1", "dimension": "creativity",
        "score": 6.0, "ts": 200, "verdict": "real",
        "judge": "deepseek-flash", "judgeVendorGroup": "deepseek",
        "sameVendorJudge": False})
    real_dir = bench_config.SCORES_DIR
    bench_config.SCORES_DIR = root
    try:
        s = summary_mod.summarize()
    finally:
        bench_config.SCORES_DIR = real_dir
    ap = s.get("judgeAperture") or {}
    check("judgesUsed 列出两 judge",
          ap.get("judgesUsed") == ["MiniMax-M3", "deepseek-flash"])
    check("sameVendorPairs=1", ap.get("sameVendorPairs") == 1)
    check("dateRange=[100, 200]", ap.get("dateRange") == [100, 200])


def main():
    test_vendor_group()
    with tempfile.TemporaryDirectory() as td:
        td = Path(td)
        (td / "a").mkdir()
        (td / "b").mkdir()
        (td / "c").mkdir()
        test_runner_writes_labels(td / "a")
        test_ingest_downweight(td / "b")
        test_summary_aperture(td / "c")
    print()
    if FAILED:
        print(f"FAIL {len(FAILED)}: {FAILED}")
        sys.exit(1)
    print("ALL GREEN")


# pytest 兼容：每个 test_*(tmp_path) 直接可用
def test_vendor_group_pytest():
    test_vendor_group()
    assert not FAILED


def test_runner_writes_labels_pytest(tmp_path):
    n0 = len(FAILED)
    test_runner_writes_labels(tmp_path)
    assert len(FAILED) == n0


def test_ingest_downweight_pytest(tmp_path):
    n0 = len(FAILED)
    test_ingest_downweight(tmp_path)
    assert len(FAILED) == n0


def test_summary_aperture_pytest(tmp_path):
    n0 = len(FAILED)
    test_summary_aperture(tmp_path)
    assert len(FAILED) == n0


if __name__ == "__main__":
    main()
