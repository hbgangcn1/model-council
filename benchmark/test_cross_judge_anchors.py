"""cross_judge 锚定 rubric 离线测试（全离线：无 API/LLM 调用，不碰 scores/）。

断言：
1. CROSS_CASES 7 题（C3 CN1 CN2 CN3 CR1 CR2 S2）每题在 ANCHOR_RUBRICS 中有条目；
2. 每题有显式扣分项（含 -2 / -1 / -0.5 三档数字）；
3. 每题有 6/8/10 分三档中文锚例；
4. build_judge_prompt 输出包含锚例 + 扣分项 + match-then-score 指示。
pytest 或 python 直接跑均可。
"""
import sys
import tempfile  # noqa: F401 (占位，保持离线测试结构一致)
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from bench import cross_judge as cj  # noqa: E402

FAILED = []
SEVEN = ["C3", "CN1", "CN2", "CN3", "CR1", "CR2", "S2"]


def check(name, cond):
    if cond:
        print(f"  ok {name}")
    else:
        print(f"  FAIL {name}")
        FAILED.append(name)


def _has_cjk(s: str) -> bool:
    return any("\u4e00" <= ch <= "\u9fff" for ch in s)


def test_anchor_coverage():
    print("[anchor coverage 7/7]")
    check("ANCHOR_RUBRICS covered == CROSS_CASES",
          set(cj.ANCHOR_RUBRICS.keys()) == set(cj.CROSS_CASES))
    for cid in SEVEN:
        check(f"{cid} in ANCHOR_RUBRICS", cid in cj.ANCHOR_RUBRICS)


def test_deductions_and_anchors():
    print("[deductions + anchors]")
    for cid in SEVEN:
        r = cj.ANCHOR_RUBRICS[cid]
        d = r.get("deductions", "")
        check(f"{cid} 扣分项含-2", "-2" in d)
        check(f"{cid} 扣分项含-1", "-1" in d)
        check(f"{cid} 扣分项含-0.5", "-0.5" in d)
        a = r.get("anchors", {})
        for lv in (6, 8, 10):
            t = a.get(lv, "")
            check(f"{cid} {lv}分锚例中文非空",
                  isinstance(t, str) and len(t) >= 20 and _has_cjk(t))
        check(f"{cid} 锚例质量梯度（6<8<10 字数递增或至少不等）",
              len(a.get(6, "")) < len(a.get(10, "")))


def test_prompt_contains_anchors():
    print("[prompt match-then-score]")
    for cid in SEVEN:
        case = {"id": cid, "prompt": "题面占位", "judge": "rubric 占位"}
        p = cj.build_judge_prompt(case, "模型回答占位")
        r = cj.ANCHOR_RUBRICS[cid]
        check(f"{cid} prompt 含6分锚例", r["anchors"][6][:20] in p)
        check(f"{cid} prompt 含10分锚例", r["anchors"][10][:20] in p)
        check(f"{cid} prompt 含扣分项", r["deductions"][:10] in p)
        check(f"{cid} prompt 含 matched-anchor 指示",
              "matched-anchor" in p or "最接近" in p)


def main():
    test_anchor_coverage()
    test_deductions_and_anchors()
    test_prompt_contains_anchors()
    print()
    if FAILED:
        print(f"FAIL {len(FAILED)}: {FAILED}")
        sys.exit(1)
    print("ALL GREEN 7/7 anchored")


def test_anchor_coverage_pytest():
    n0 = len(FAILED)
    test_anchor_coverage()
    assert len(FAILED) == n0


def test_deductions_and_anchors_pytest():
    n0 = len(FAILED)
    test_deductions_and_anchors()
    assert len(FAILED) == n0


def test_prompt_contains_anchors_pytest():
    n0 = len(FAILED)
    test_prompt_contains_anchors()
    assert len(FAILED) == n0


if __name__ == "__main__":
    main()
