"""P2-3：golden 隔离舱守卫（golden_guard.py）。

契约（2026-08-24 元评审 P2-3；v15.4 全自动流水线修订）：
1. 每条 golden item 必须带 provenance（author/curator/source/excludedFromCandidateCorpus）；
2. source ∈ {public-generic, domain-private, synthetic-new}，
   domain-private + synthetic-new 占比 ≥ 30%（异源比例，防 judge 训练分布重叠）；
3. golden 的 task/answer 不得与 v21-cases.json 任何 prompt 重合（防 judge 对基准题"见过"）；
4. contentHash = sha256(规范化 items)，被改可检测（judge_drift 基线同样锁定该哈希）；
5. v15.4：author 允许 "ai"（golden_evolve 全自动流水线产物），但必须带 reviewedBy
   （≥2 家异厂商复核模型，结论 pass）；有争议条目须带 arbitration 记录。

用法：
  python benchmark/golden_guard.py          # 校验（退出码 1 = 违反契约）
"""
import hashlib
import json
import sys
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent  # council/
GOLDEN = BASE / "benchmark" / "golden" / "golden-set.json"
V21 = BASE / "benchmark" / "v21-cases.json"

ALLOWED_SOURCES = {"public-generic", "domain-private", "synthetic-new"}
MIN_HETERO_PCT = 30.0
ALLOWED_AUTHORS = {"human", "ai"}          # v15.4：ai 条目须带 reviewedBy ≥2 家异厂商


def _canonical(obj) -> str:
    return json.dumps(obj, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def items_hash(items: list) -> str:
    return hashlib.sha256(_canonical(items).encode("utf-8")).hexdigest()


def check_doc(doc: dict, cases_doc: dict = None) -> list:
    """返回问题列表；空 = 通过。cases_doc 为 v21-cases.json（可选，做无交集检查）。"""
    problems = []
    items = doc.get("items")
    if not isinstance(items, list) or not items:
        return ["items 缺失或为空"]
    # 1. provenance 契约
    for it in items:
        iid = it.get("id", "?")
        prov = it.get("provenance")
        if not isinstance(prov, dict):
            problems.append(f"{iid}: 缺 provenance")
            continue
        if prov.get("author") not in ALLOWED_AUTHORS:
            problems.append(f"{iid}: provenance.author 应在 {sorted(ALLOWED_AUTHORS)}")
        if prov.get("source") not in ALLOWED_SOURCES:
            problems.append(f"{iid}: source 不在 {sorted(ALLOWED_SOURCES)}")
        if prov.get("excludedFromCandidateCorpus") is not True:
            problems.append(f"{iid}: excludedFromCandidateCorpus 必须为 true")
        if not isinstance(it.get("holdout"), bool):
            problems.append(f"{iid}: 缺 holdout 布尔标记")
        # v15.5 评卷考场契约：expectedScore / badAnswers / machineCheckable
        es = it.get("expectedScore")
        if not (isinstance(es, (int, float)) and 0 <= es <= 10):
            problems.append(f"{iid}: expectedScore 应在 [0,10]（v15.5 评卷考场）")
        bads = it.get("badAnswers")
        if not isinstance(bads, list) or not bads:
            problems.append(f"{iid}: badAnswers 需 ≥1 个劣质变体（v15.5 评卷考场）")
        else:
            for b in bads:
                if not isinstance(b, dict) or not b.get("text") or not b.get("label") \
                        or not (isinstance(b.get("expectedScore"), (int, float)) and 0 <= b["expectedScore"] <= 10):
                    problems.append(f"{iid}: badAnswers 每条需 label/text/expectedScore∈[0,10]")
        if not isinstance(it.get("machineCheckable"), bool):
            problems.append(f"{iid}: 缺 machineCheckable 布尔标记")
        # v15.4：AI 条目必须带异厂商复核记录；存在非 pass 复核时须有仲裁通过记录
        if prov.get("author") == "ai":
            reviewed = prov.get("reviewedBy") or []
            if len(reviewed) < 2:
                problems.append(f"{iid}: ai 条目 reviewedBy 需 ≥2 家复核")
            else:
                vendors = {r.get("vendor") for r in reviewed if isinstance(r, dict) and r.get("vendor")}
                if len(vendors) < 2:
                    problems.append(f"{iid}: ai 条目 reviewedBy 需 ≥2 家异厂商（当前 {sorted(vendors) if vendors else '无'}）")
                has_fail = any(r.get("verdict") != "pass" for r in reviewed if isinstance(r, dict))
                if has_fail:
                    arb = prov.get("arbitration") or {}
                    if not (arb.get("verdict") == "pass" and arb.get("vendor")):
                        problems.append(f"{iid}: ai 条目存在非 pass 复核且无仲裁通过记录（应被仲裁或淘汰）")
    # 2. 异源比例 ≥30%
    n = len(items)
    hetero = sum(1 for it in items
                 if (it.get("provenance") or {}).get("source") in
                 {"domain-private", "synthetic-new"})
    if hetero / n * 100 < MIN_HETERO_PCT:
        problems.append(f"异源比例 {hetero}/{n}={hetero / n * 100:.0f}% < {MIN_HETERO_PCT:.0f}%")
    # v15.5 A1 机器判分锚保底：machineCheckable 占比 ≥ 25%
    mc = sum(1 for it in items if it.get("machineCheckable"))
    if mc / n * 100 < 25.0:
        problems.append(f"machineCheckable 占比 {mc}/{n}={mc / n * 100:.0f}% < 25%（A1 机器判分锚保底）")
    # 3. contentHash 校验（字段存在时）
    if doc.get("contentHash"):
        if doc["contentHash"] != items_hash(items):
            problems.append("contentHash 与 items 不符（金标被改动或哈希未更新）")
    # 4. 与 v21-cases.json 无交集
    if cases_doc:
        case_prompts = [c.get("prompt", "") for c in cases_doc.get("cases", []) if isinstance(c, dict)]
        norm = lambda s: "".join(str(s).split()).lower()  # noqa: E731
        norm_prompts = {norm(p) for p in case_prompts if p}
        for it in items:
            iid = it.get("id", "?")
            t = norm(it.get("task", ""))
            a = norm(it.get("answer", ""))
            for p in norm_prompts:
                if t and (t in p or p in t):
                    problems.append(f"{iid}: task 与用例 prompt 重合")
                    break
                if a and (a in p or p in a):
                    problems.append(f"{iid}: answer 与用例 prompt 重合")
                    break
    return problems


def check_file() -> list:
    doc = json.loads(GOLDEN.read_text(encoding="utf-8"))
    cases_doc = None
    if V21.exists():
        try:
            cases_doc = json.loads(V21.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            pass
    return check_doc(doc, cases_doc)


if __name__ == "__main__":
    problems = check_file()
    if problems:
        for p in problems:
            print(f"❌ {p}")
        sys.exit(1)
    print("✅ golden 契约全部通过")
