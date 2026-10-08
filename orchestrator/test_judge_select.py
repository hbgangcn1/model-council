"""judge_select 离线证明测试：全 fixtures，不调 API（无网络、无模型名硬编码依赖）。

证明三件事：
1. pool={A,B} 未知名时，选中项只来自输入（无字面模型默认值）；
2. hetero ban 被尊重（被 ban 者不出排名，全 ban 返回 [] 不 crash）；
3. 评卷数据为空时按能力均值回退，不 crash。
"""
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from orchestrator import judge_select  # noqa: E402

FAILED = []


def check(name, cond):
    if cond:
        print(f"  ✓ {name}")
    else:
        print(f"  ✗ {name}")
        FAILED.append(name)


def _caps(a_scores, b_scores):
    def _entry(base, scores):
        return {"baseModel": base, "thinking": "low", "stable": True,
                "capabilities": {f"d{i}": {"score": s, "samples": 10}
                                 for i, s in enumerate(scores)}}
    return {"models": {"A__low": _entry("A", a_scores),
                       "B__low": _entry("B", b_scores)}}


def _profiles_with(scores_by_model):
    """scores_by_model: {model: judgingScore}；None 值模拟 judge_qualify 未写分。"""
    return {"schemaVersion": 1,
            "judges": {m: ({"judgingScore": s} if s is not None else {})
                       for m, s in scores_by_model.items()}}


def _run_with_profiles(profiles, fn):
    """把 judge_select 的档案源临时换成内存 fixture（不碰磁盘）。"""
    orig = judge_select._load_json
    judge_select._load_json = lambda p: profiles  # noqa: E731
    try:
        return fn()
    finally:
        judge_select._load_json = orig


def test_unknown_pool_no_literal_default():
    print("[未知池无字面默认值]")
    caps = _caps([9.0, 9.0], [5.0, 5.0])
    ranked = _run_with_profiles({"judges": {}},
                                lambda: judge_select.select_judge(["A", "B"], caps, [], None))
    check("A（能力均值高）排第一", ranked and ranked[0] == ("A", "low"))
    check("结果只含池内模型", all(m in ("A", "B") for m, _ in ranked))
    src = Path(judge_select.__file__).read_text(encoding="utf-8")
    check("judge_select 无字面 MiniMax 默认", "MiniMax-M3" not in src)
    check("judge_select 无字面 deepseek 默认", "deepseek-v4-flash" not in src)


def test_hetero_ban_respected():
    print("[hetero ban]")
    caps = _caps([9.0, 9.0], [8.0, 8.0])
    r1 = _run_with_profiles({"judges": {}},
                            lambda: judge_select.select_judge(["A", "B"], caps, ["A"], None))
    check("被 ban 的 A 不出排名", all(m != "A" for m, _ in r1))
    check("未 ban 的 B 仍可选", ("B", "low") in r1)
    r2 = _run_with_profiles({"judges": {}},
                            lambda: judge_select.select_judge(["A", "B"], caps, ["A", "B"], None))
    check("全 ban 返回 [] 不 crash", r2 == [])


def test_empty_judging_fallback():
    print("[空评卷数据回退]")
    caps = _caps([4.0, 4.0], [7.0, 7.0])
    for profiles in ({"judges": {}}, {"schemaVersion": 1}, None, {"judges": {"A": {}}}):
        ranked = _run_with_profiles(
            profiles, lambda: judge_select.select_judge(["A", "B"], caps, [], None))
        check("空档案下 B（能力均值高）第一且不 crash", ranked and ranked[0] == ("B", "low"))


def test_judging_score_overrides_caps():
    print("[评卷分优先于能力均值]")
    caps = _caps([9.0, 9.0], [5.0, 5.0])  # 能力上 A 更强
    profiles = _profiles_with({"A": 1.0, "B": 8.0})  # 评卷上 B 更强
    ranked = _run_with_profiles(
        profiles, lambda: judge_select.select_judge(["A", "B"], caps, [], None))
    check("评卷分高的 B 排第一", ranked and ranked[0][0] == "B")


def test_missing_judging_field_derived():
    print("[缺 judgingScore 现场推导]")
    caps = _caps([5.0, 5.0], [5.0, 5.0])
    profiles = {"judges": {"A": {"calibrationError": 0.5, "discrimination": 6.0},
                           "B": {"calibrationError": 3.0, "discrimination": 2.0}}}
    ranked = _run_with_profiles(
        profiles, lambda: judge_select.select_judge(["A", "B"], caps, [], None))
    check("由 calib/disc 推导：A 第一", ranked and ranked[0][0] == "A")


def test_quota_snapshot_fail_open():
    print("[额度快照 fail-open]")
    caps = _caps([9.0, 9.0], [5.0, 5.0])
    ranked = _run_with_profiles(
        {"judges": {}},
        lambda: judge_select.select_judge(["A", "B"], caps, [], {"exhausted": ["A"]}))
    check("耗尽的 A 沉底但不剔除", ranked and ranked[-1][0] == "A" and len(ranked) == 2)
    ranked2 = _run_with_profiles(
        {"judges": {}},
        lambda: judge_select.select_judge(["A", "B"], caps, [], "garbage"))
    check("坏快照忽略不 crash", ranked2 and ranked2[0][0] == "A")


def main():
    test_unknown_pool_no_literal_default()
    test_hetero_ban_respected()
    test_empty_judging_fallback()
    test_judging_score_overrides_caps()
    test_missing_judging_field_derived()
    test_quota_snapshot_fail_open()
    print()
    if FAILED:
        print(f"❌ {len(FAILED)} 项失败: {FAILED}")
        sys.exit(1)
    print("✅ 全部通过")


if __name__ == "__main__":
    main()
