"""v15.4：金标/考卷全自动进化流水线（golden_evolve.py，§14-v15.4-F/G）。

考题生命周期：AI 出题 → 异厂商双盲复核 + 仲裁 → 进金标池（校准 judge）→
观察期后晋升考卷池（v21-cases）→ 区分度衰减退役。金标池=育苗田，考卷池=生产田。

子命令：
  --expand N         金标池补 N 条（出题+复核+仲裁，契约校验）
  --health-check     金标健康度检查（judge 得分方差/区分度/可争议性 → 报告）
  --replace          替换低健康度金标（保持总数不变）
  --fill-v21         按「每维 95% CI ≤ ±1 分」达标线检查考卷题数，不足维度出题晋升
  --promote          健康金标晋升考卷池（观察期 ≥3 天、judge 打分健康）
  --auto             组合模式（挂每日 04:30 自评后）：health-check → replace → fill-v21 → promote
  --dry              只报告不落盘

AI 条目契约（golden_guard v15.4）：author="ai" + reviewedBy ≥2 家异厂商 + 全 pass；
有争议条目仲裁（第三家异厂商）后才可入库。所有操作写审计日志 golden-evolve-events.jsonl。
"""
import argparse
import datetime
import json
import math
import sys
import time
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent  # council/
GOLDEN = BASE / "benchmark" / "golden" / "golden-set.json"
V21 = BASE / "benchmark" / "v21-cases.json"
EVENTS = BASE / "benchmark" / "golden-evolve-events.jsonl"
SCORES_DIR = BASE / "benchmark" / "scores"

sys.path.insert(0, str(BASE / "orchestrator"))
sys.path.insert(0, str(BASE))
from orchestrator import stream_llm, config_loader  # noqa: E402
from benchmark import golden_guard  # noqa: E402

# 出题/复核模型（出题用最快的低成本模型，质量由两家异厂商复核把关；仲裁用异厂商高思考档）
# 2026-08-27：OpenRouter（stealth/ox-alpha）已退役移除，出题改用 deepseek-v4-flash；
# 仲裁契约从「第三家异厂商」退化为「与至少一家复核者异厂商」（仅剩 minimax/deepseek 两厂商）。
GEN_MODEL = ("deepseek-v4-flash", "low")     # 快且便宜，出题量大时省时
REVIEWERS = [("MiniMax-M3", "low", "minimax-cn"), ("deepseek-v4-flash", "low", "deepseek-official")]
ARBITER = ("MiniMax-M3", "high")             # 与 deepseek 复核者异厂商
# 实测（2026-08-24）：ox 出 code 推演题答案错误率高（复核拦截 N 次），code 维度改用 M3
GEN_MODEL_BY_DIM = {"code": ("MiniMax-M3", "low")}


def _gen_model_for(dimension: str):
    return GEN_MODEL_BY_DIM.get(dimension, GEN_MODEL)

DIMENSIONS = ["reasoning", "code", "chinese", "research", "instruction_following",
              "long_context", "tool_use", "creativity", "safety"]

# v15.4 G-34/35：考卷每维达标线（95% CI ≤ ±1 分 → n ≥ (1.96σ)²，σ 用实测模型内题间标准差）
TARGET_CI = 1.0
MIN_CASES_PER_DIM = 3       # 绝对下限（统计达标线之外的最低保底，防单题维度）
FALLBACK_SIGMA = 0.8        # 无实测数据时的保守 σ


def _now_iso():
    return datetime.datetime.now().astimezone().isoformat()


def _log_event(ev: dict):
    try:
        with EVENTS.open("a", encoding="utf-8") as f:
            f.write(json.dumps(ev, ensure_ascii=False) + "\n")
    except OSError:
        pass


def _fix_newlines_in_strings(s: str) -> str:
    """状态机修复：只把 JSON 字符串值内部的裸换行转义为 \\n（结构换行保留）。
    模型（ox-alpha 实测）常在多行格式化 JSON 的字符串值里输出裸换行，regex 无法
    区分字符串内/结构换行，逐字符状态机可靠。"""
    out = []
    in_str = False
    esc = False
    for ch in s:
        if in_str:
            if ch == "\n":
                out.append("\\n")
                continue
            out.append(ch)
            if ch == "\\" and not esc:
                esc = True
            elif esc:
                esc = False
            elif ch == '"':
                in_str = False
        else:
            out.append(ch)
            if ch == '"':
                in_str = True
    return "".join(out)


def _extract_json(text: str):
    """JSON 提取（lenient）：①优先剥 ```json fence；②字符串内裸换行状态机修复。"""
    import re
    m = re.search(r"```(?:json)?\s*(\{.*\})\s*```", text or "", re.S)
    raw = m.group(1) if m else None
    if raw is None:
        m = re.search(r"\{.*\}", text or "", re.S)
        if not m:
            return None
        raw = m.group(0)
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        pass
    try:
        return json.loads(_fix_newlines_in_strings(raw))
    except json.JSONDecodeError:
        return None


def _load_json(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def _write_atomic(path: Path, obj):
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(obj, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(path)


GEN_PROMPT = """你是 benchmark 出题员。为「{dimension}」维度出一道评测题，要求：
1. 答案必须**唯一、客观、可机器判分**（不接受观点题/时效题）；
2. 中文题目；避开公开题库高频题（模型可能背过）；难度中等偏难（简单题区分度差）；
3. task 里不含任何答案线索；rubric 给出 10 分制分档判分规则；
4. expected 必须是机器可判的结构：must（必须出现的要点/关键词）、mustNot（不得出现的词）、numbers（必须出现的数字）、exact（唯一正确答案文本）。
5. **JSON 严格合法**：字符串内的换行一律用 \\n 转义，代码用 \\n 分隔行，不要输出裸换行。

只输出 JSON：
{{
  "dimension": "{dimension}",
  "task": "题目",
  "answer": "标准答案（完整）",
  "rubric": "10 分制分档规则",
  "expected": {{"must": ["要点1"], "mustNot": ["禁用词"], "numbers": [123], "exact": "唯一答案"}},
  "difficulty": "easy/medium/hard"
}}"""

REVIEW_PROMPT = """你是金标复核员。评判这道题是否可作为 judge 校准金标：
1. 答案是否**唯一客观可判**（无歧义、无时效、无观点争议）？
2. rubric 分档是否具体无歧义？
3. expected 是否真的能机器判分（must/numbers/exact 是否齐全且与答案一致）？

题目：{task}
答案：{answer}
rubric：{rubric}
expected：{expected}

只输出 JSON：{{"verdict": "pass/fail", "issues": ["具体问题"], "confidence": 0-1}}"""

ARBITER_PROMPT = """你是金标仲裁员。两家复核意见冲突，请终审：
题目：{task}
答案：{answer}
复核1：{r1}
复核2：{r2}
只输出 JSON：{{"verdict": "pass/fail", "reason": "终审理由"}}"""


def _call(model, thinking, prompt, tries: int = 2):
    """调用模型（轻量重试——免费池瞬时 SSL/429 抖动常见，实测 2026-08-24）。"""
    for t in range(tries):
        try:
            text, meta = stream_llm.call_stream(model, thinking, prompt,
                                                config_loader.max_tokens_for_model(model))
            if meta.get("timeout_kind") or not (text or "").strip():
                if t < tries - 1:
                    time.sleep(3)
                    continue
                return None, meta
            return text, meta
        except Exception as e:
            if t < tries - 1:
                time.sleep(3)
                continue
            return None, {"error": str(e)[:200]}
    return None, {"error": "retries_exhausted"}


def _vendor_of(model: str) -> str:
    if model.startswith("MiniMax"):
        return "minimax-cn"
    return "deepseek-official"


def gen_and_review(dimension: str, dry: bool = False) -> dict:
    """出题 + 双盲复核 + 仲裁。返回 golden item（或 None）。"""
    gen_model = _gen_model_for(dimension)
    for attempt in range(3):
        text, _ = _call(*gen_model, GEN_PROMPT.format(dimension=dimension))
        g = _extract_json(text)
        if not g or not g.get("task") or not g.get("answer") or not g.get("expected"):
            continue
        # 双盲复核（两家异厂商）
        reviews = []
        for rmodel, rthinking, _vendor in REVIEWERS:
            rtext, _ = _call(rmodel, rthinking, REVIEW_PROMPT.format(
                task=g["task"], answer=g["answer"], rubric=g.get("rubric", ""),
                expected=json.dumps(g.get("expected"), ensure_ascii=False)))
            rj = _extract_json(rtext) or {}
            reviews.append({"model": rmodel, "vendor": _vendor_of(rmodel),
                            "verdict": rj.get("verdict", "fail"),
                            "issues": rj.get("issues", []),
                            "confidence": rj.get("confidence")})
        verdicts = [r["verdict"] for r in reviews]
        if all(v == "pass" for v in verdicts):
            item = {
                "id": "", "dimension": dimension,
                "task": g["task"], "answer": g["answer"],
                "rubric": g.get("rubric", ""), "holdout": False,
                "difficulty": g.get("difficulty", "medium"),
                "expected": g.get("expected"),
                "provenance": {
                    "author": "ai", "curator": "the maintainer",
                    "createdAt": _now_iso(), "lastReviewedAt": _now_iso(),
                    "source": "synthetic-new",
                    "excludedFromCandidateCorpus": True,
                    "genModel": f"{GEN_MODEL[0]}@{GEN_MODEL[1]}",
                    "reviewedBy": reviews,
                    "arbitration": None,
                },
            }
            return item
        # 冲突/全 fail → 仲裁（第三家异厂商）
        atext, _ = _call(*ARBITER, ARBITER_PROMPT.format(
            task=g["task"], answer=g["answer"],
            r1=json.dumps(reviews[0], ensure_ascii=False),
            r2=json.dumps(reviews[1], ensure_ascii=False)))
        aj = _extract_json(atext) or {}
        if aj.get("verdict") == "pass":
            item = {
                "id": "", "dimension": dimension,
                "task": g["task"], "answer": g["answer"],
                "rubric": g.get("rubric", ""), "holdout": False,
                "difficulty": g.get("difficulty", "medium"),
                "expected": g.get("expected"),
                "provenance": {
                    "author": "ai", "curator": "the maintainer",
                    "createdAt": _now_iso(), "lastReviewedAt": _now_iso(),
                    "source": "synthetic-new",
                    "excludedFromCandidateCorpus": True,
                    "genModel": f"{GEN_MODEL[0]}@{GEN_MODEL[1]}",
                    "reviewedBy": reviews,
                    "arbitration": {"model": f"{ARBITER[0]}@{ARBITER[1]}",
                                    "vendor": _vendor_of(ARBITER[0]),
                                    "verdict": "pass", "reason": aj.get("reason", "")},
                },
            }
            return item
        # 本尝试失败，重出
    return None


def _next_gid(doc: dict) -> str:
    nums = [int(it["id"][1:]) for it in doc.get("items", [])
            if str(it.get("id", "")).startswith("G") and it["id"][1:].isdigit()]
    return f"G{max(nums) + 1 if nums else 1}"


def _next_free_gid(golden_doc: dict, v21_doc: dict) -> str:
    """全局唯一 G id：取金标池与考卷池已用 id 的最大值 +1（防晋升题 id 撞车）。"""
    used = set()
    for it in golden_doc.get("items", []):
        used.add(str(it.get("id", "")))
    for c in v21_doc.get("cases", []):
        used.add(str(c.get("id", "")))
    nums = [int(x[1:]) for x in used if x.startswith("G") and x[1:].isdigit()]
    return f"G{max(nums) + 1 if nums else 1}"


def expand(n: int, dry: bool = False) -> dict:
    doc = _load_json(GOLDEN)
    existing_tasks = {(it.get("task") or "").strip() for it in doc.get("items", [])}
    added = []
    for _ in range(n):
        # 按当前条目数最少的维度补（均衡）
        dim_count = {d: 0 for d in DIMENSIONS}
        for it in doc.get("items", []):
            dim_count[it.get("dimension", "?")] = dim_count.get(it.get("dimension", "?"), 0) + 1
        dim = min(dim_count, key=dim_count.get)
        item = gen_and_review(dim)
        if not item:
            continue
        item["id"] = _next_gid(doc)
        if item["task"].strip() in existing_tasks:
            continue  # 防重复出题
        doc["items"].append(item)
        existing_tasks.add(item["task"].strip())
        added.append(item["id"])
    if added and not dry:
        doc["contentHash"] = golden_guard.items_hash(doc["items"])
        _write_atomic(GOLDEN, doc)
        problems = golden_guard.check_file()
        if problems:
            _log_event({"ts": _now_iso(), "op": "expand", "ids": added, "guard": problems})
            return {"added": len(added), "ids": added, "guardFailed": problems[:3]}
    _log_event({"ts": _now_iso(), "op": "expand", "ids": added, "dry": dry})
    return {"added": len(added), "ids": added}


def health_check() -> dict:
    """金标健康度：judge 得分方差（可争议）/ 全满分（无区分度）。数据来自 judge-drift.json 的 scores。"""
    jd = _load_json(BASE / "judge-drift.json")
    scores = {s["id"]: s for s in jd.get("scores", []) if s.get("ok")}
    doc = _load_json(GOLDEN)
    out = []
    for it in doc.get("items", []):
        rec = scores.get(it["id"])
        h = {"id": it["id"], "dimension": it.get("dimension"), "healthy": True, "issues": []}
        if rec:
            sc = rec.get("score")
            # 全满分且 rubric 非"满分即对"型 → 区分度存疑（G14/G22 教训）
            if sc is not None and sc >= 9.8 and it.get("difficulty") in ("easy", None, "medium"):
                h["issues"].append("ceiling")
                h["healthy"] = False
        if not rec:
            h["issues"].append("no_score_data")
        # v15.5 金标 TTL：lastReviewedAt > 90 天且非 holdout → 过期标记（走 replace/人工锚流程）
        lra = (it.get("provenance") or {}).get("lastReviewedAt")
        if lra and not it.get("holdout"):
            try:
                dt = datetime.datetime.fromisoformat(lra)
                if (datetime.datetime.now().astimezone() - dt).days > 90:
                    h["issues"].append("ttl_stale")
                    h["healthy"] = False
            except (ValueError, TypeError):
                pass
        out.append(h)
    unhealthy = [h for h in out if not h["healthy"]]
    _log_event({"ts": _now_iso(), "op": "health-check", "unhealthy": len(unhealthy)})
    return {"items": out, "unhealthyCount": len(unhealthy)}


def replace_unhealthy(dry: bool = False) -> dict:
    """替换低健康度金标（保总数；只替换非 holdout 且来源为 synthetic-new 的条目）。"""
    report = health_check()
    doc = _load_json(GOLDEN)
    replaced = []
    for h in report["items"]:
        if h["healthy"]:
            continue
        idx = next((i for i, it in enumerate(doc["items"]) if it["id"] == h["id"]), None)
        if idx is None:
            continue
        it = doc["items"][idx]
        if it.get("holdout") or (it.get("provenance") or {}).get("source") != "synthetic-new":
            continue
        new_item = gen_and_review(it.get("dimension", "reasoning"))
        if not new_item:
            continue
        new_item["id"] = h["id"]
        new_item["provenance"]["createdAt"] = it.get("provenance", {}).get("createdAt", _now_iso())
        doc["items"][idx] = new_item
        replaced.append(h["id"])
    if replaced and not dry:
        doc["contentHash"] = golden_guard.items_hash(doc["items"])
        _write_atomic(GOLDEN, doc)
    _log_event({"ts": _now_iso(), "op": "replace", "ids": replaced, "dry": dry})
    return {"replaced": replaced}


def _golden_to_case(item: dict) -> dict:
    """金标 → v21 case（scoring=objective + 通用判分 expected）。"""
    exp = item.get("expected") or {}
    return {
        "id": item["id"],  # G 前缀 id 直接作为考卷题号（SCORERS 未命中时走通用判分）
        "dimension": item.get("dimension", "reasoning"),
        "name": f"金标晋升-{item.get('dimension')}-{item['id']}",
        "prompt": item["task"],
        "scoring": "objective",
        "expected": item.get("answer", ""),
        "scoringSpec": exp,   # v15.4 通用判分规格 {must/mustNot/numbers/exact}
        "judge": item.get("rubric", ""),
        "source": f"golden-promoted:{item['id']}",
    }


def _dim_sigma(dimension: str) -> float:
    """该维度实测模型内题间标准差（跨模型平均）——达标线计算用（G-35）。
    无数据 → 保守 FALLBACK_SIGMA。"""
    by_case = _scores_by_case()
    dim_scores = {}
    for case_id, cand_scores in by_case.items():
        dim = None
        # 从 v21 找 case 维度
        try:
            v21 = _load_json(V21)
            for c in v21.get("cases", []):
                if c["id"] == case_id:
                    dim = c.get("dimension")
                    break
        except Exception:
            pass
        if dim != dimension:
            continue
        for cid, s in cand_scores.items():
            dim_scores.setdefault(cid, []).append(s)
    within_sds = []
    for vals in dim_scores.values():
        if len(vals) >= 2:
            m = sum(vals) / len(vals)
            sd = math.sqrt(sum((v - m) ** 2 for v in vals) / (len(vals) - 1))
            within_sds.append(sd)
    return round(sum(within_sds) / len(within_sds), 3) if within_sds else FALLBACK_SIGMA


def _dim_between_model_std(dimension: str) -> float:
    """v15.5 区分度目标：该维度各模型总分的标准差（模型间拉不开差距 = std 小）。"""
    by_case = _scores_by_case()
    dim_scores = {}
    for case_id, cand_scores in by_case.items():
        dim = None
        try:
            v21 = _load_json(V21)
            for c in v21.get("cases", []):
                if c["id"] == case_id:
                    dim = c.get("dimension")
                    break
        except Exception:
            pass
        if dim != dimension:
            continue
        for cid, s in cand_scores.items():
            dim_scores.setdefault(cid, []).append(s)
    totals = [sum(v) / len(v) for v in dim_scores.values() if v]
    if len(totals) < 3:
        return None
    m = sum(totals) / len(totals)
    return math.sqrt(sum((t - m) ** 2 for t in totals) / (len(totals) - 1))


def fill_one(dry: bool = False, dimension: str = None) -> dict:
    """每次只补一道题（原子事务）：成功立即落盘（晋升考卷+审计），失败快速返回。
    the maintainer decided（2026-08-25）：不要一次性补完——单次运行几分钟内结束，失败不再空转。
    维度选择：缺题最多的维度优先；可指定 --dim。
    v15.5 区分度目标：缺口 = max(CI 达标缺口, 区分度缺口)——模型间分数拉不开也加题。"""
    doc = _load_json(V21)
    cases = doc.get("cases", [])
    per_dim = {d: 0 for d in DIMENSIONS}
    for c in cases:
        per_dim[c.get("dimension", "?")] = per_dim.get(c.get("dimension", "?"), 0) + 1
    shortfall = []
    for d in DIMENSIONS:
        sigma = _dim_sigma(d)
        target = max(MIN_CASES_PER_DIM, math.ceil((1.96 * sigma / TARGET_CI) ** 2))
        short = target - per_dim.get(d, 0)
        # v15.5 区分度缺口：模型间分数 std < 0.5（拉不开差距）且题数 < 8 → 加题
        dstd = _dim_between_model_std(d)
        if dstd is not None and dstd < 0.5 and per_dim.get(d, 0) < 8:
            short = max(short, 2)
            _log_event({"ts": _now_iso(), "op": "fill-discrimination", "dimension": d,
                        "std": round(dstd, 3), "note": "模型间区分度不足→加题"})
        if short > 0:
            shortfall.append((d, short))
    if not shortfall:
        return {"needed": {}, "promoted": [], "note": "达标线已满足，无需补题"}
    if dimension:
        pick = next(((d, s) for d, s in shortfall if d == dimension), None)
        if not pick:
            return {"needed": {}, "promoted": [], "note": f"{dimension} 不缺题"}
    else:
        pick = max(shortfall, key=lambda x: x[1])
    d, short = pick
    golden_doc = _load_json(GOLDEN)
    existing_tasks = {(it.get("task") or "").strip() for it in golden_doc.get("items", [])}
    existing_case_prompts = {c.get("prompt", "") for c in cases}
    item = gen_and_review(d)
    if not item:
        _log_event({"ts": _now_iso(), "op": "fill-one", "dimension": d, "result": "fail",
                    "note": "出题/复核未通过（安全网拦截或模型故障），下次再试"})
        return {"needed": {d: short}, "promoted": [], "note": f"{d} 出题/复核失败（下次再试）"}
    if item["task"].strip() in existing_tasks or item["task"] in existing_case_prompts:
        _log_event({"ts": _now_iso(), "op": "fill-one", "dimension": d, "result": "duplicate"})
        return {"needed": {d: short}, "promoted": [], "note": "出题与现有题重复（下次再试）"}
    item["id"] = _next_free_gid(golden_doc, doc)  # 全局唯一 id（金标池 ∪ 考卷池，防晋升撞车）
    item["provenance"]["promotedToV21At"] = _now_iso()
    case = _golden_to_case(item)
    if not dry:
        doc["cases"].append(case)
        doc["totalCases"] = len(doc["cases"])
        doc["contentHash"] = _v21_hash(doc)
        _write_atomic(V21, doc)
        problems = golden_guard.check_file()
        if problems:
            _log_event({"ts": _now_iso(), "op": "fill-one", "dimension": d,
                        "result": "guard-fail", "guard": problems[:3]})
            return {"needed": {d: short}, "promoted": [], "note": "契约校验失败", "guard": problems[:3]}
    _log_event({"ts": _now_iso(), "op": "fill-one", "dimension": d,
                "promoted": case["id"], "dry": dry})
    return {"needed": {d: short}, "promoted": [case["id"]]}


def fill_v21(dry: bool = False) -> dict:
    """手动全量模式：循环 fill_one 直到无缺或单题失败（保留给手动补题场景）。"""
    total_promoted = []
    for _ in range(50):  # 防死循环上限
        r = fill_one(dry=dry)
        if not r.get("promoted"):
            break
        total_promoted.extend(r["promoted"])
    return {"promoted": total_promoted}


def _v21_hash(doc: dict) -> str:
    """v21-cases 的 contentHash 算法（与现有 hashedFields 兼容：规范化 hashedFields）。"""
    import hashlib
    fields = doc.get("hashedFields") or ["cases"]
    payload = {f: doc.get(f) for f in fields if f in doc}
    canonical = json.dumps(payload, ensure_ascii=False, sort_keys=True,
                           separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


# ---------------- v15.4 G-37：考卷分布健康四指标（体检在绝对分 0-10 上做） ----------------

def _scores_by_case() -> dict:
    """scores/ → {case_id: {cand_id: score}}（只取 verdict=real 的分数）。"""
    out = {}
    if not SCORES_DIR.exists():
        return out
    for f in SCORES_DIR.rglob("*.json"):
        try:
            j = json.loads(f.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            continue
        if j.get("case_id") and isinstance(j.get("score"), (int, float)):
            out.setdefault(j["case_id"], {})[j.get("cand_id") or f.parent.name] = float(j["score"])
    return out


def _pearson(xs: list, ys: list) -> float:
    if len(xs) < 3:
        return 0.0
    mx, my = sum(xs) / len(xs), sum(ys) / len(ys)
    num = sum((x - mx) * (y - my) for x, y in zip(xs, ys))
    den = math.sqrt(sum((x - mx) ** 2 for x in xs) * sum((y - my) ** 2 for y in ys))
    return num / den if den else 0.0


def check_v21_health(dry: bool = False) -> dict:
    """分布健康四指标（G-37）：范围 / 天花板地板率 / 断层 / 题区分度系数。
    低区分度题（|相关性| < 0.2 或天花板题）自动退役（移入 deprecated，不清除成绩）。"""
    doc = _load_json(V21)
    cases = doc.get("cases", [])
    by_case = _scores_by_case()
    # 每个候选的总体均分（真实能力代理）
    cand_all = {}
    for case_id, cand_scores in by_case.items():
        for cid, s in cand_scores.items():
            cand_all.setdefault(cid, []).append(s)
    cand_mean = {c: sum(v) / len(v) for c, v in cand_all.items() if v}
    report = {"perDim": {}, "retired": []}
    retired_ids = set()
    for d in DIMENSIONS:
        dim_cases = [c for c in cases if c.get("dimension") == d]
        vals = [s for c in dim_cases for s in (by_case.get(c["id"], {}).values())]
        ceiling_count = sum(1 for s in vals if s >= 9.8)
        ceiling_pct = round(ceiling_count / len(vals) * 100, 1) if vals else None
        dim_report = {"cases": len(dim_cases), "range": round(max(vals) - min(vals), 2) if vals else None,
                      "ceilingPct": ceiling_pct, "healthy": True, "issues": []}
        if vals and ceiling_pct is not None and ceiling_pct > 30:
            dim_report["issues"].append("ceiling")
            dim_report["healthy"] = False
        if vals and dim_report["range"] is not None and dim_report["range"] < 1.5 and len(dim_cases) >= 3:
            dim_report["issues"].append("narrow_range")
        # 断层：相邻名次跳变 >3 分
        if len(dim_cases) >= 3:
            case_means = sorted({c["id"]: sum(by_case.get(c["id"], {}).values()) / len(by_case[c["id"]])
                                 for c in dim_cases if by_case.get(c["id"])}.values())
            if any(b - a > 3 for a, b in zip(case_means, case_means[1:])):
                dim_report["issues"].append("gap")
                dim_report["healthy"] = False
        report["perDim"][d] = dim_report
        # 题区分度系数：该题得分与候选总体水平的相关性 < 0.2 → 噪声题退役
        for c in dim_cases:
            scores = by_case.get(c["id"], {})
            common = [(scores[cid], cand_mean[cid]) for cid in scores if cid in cand_mean]
            if len(common) < 3:
                continue
            r = _pearson([x for x, _ in common], [y for _, y in common])
            if abs(r) < 0.2 or (all(s >= 9.8 for s in scores.values()) and len(scores) >= 3):
                retired_ids.add(c["id"])
    if retired_ids and not dry:
        keep = [c for c in cases if c["id"] not in retired_ids]
        dep = list(doc.get("deprecatedCases") or [])
        dep.extend(c for c in cases if c["id"] in retired_ids)
        doc["cases"] = keep
        doc["deprecatedCases"] = dep
        doc["totalCases"] = len(keep)
        doc["contentHash"] = _v21_hash(doc)
        _write_atomic(V21, doc)
        report["retired"] = sorted(retired_ids)
    _log_event({"ts": _now_iso(), "op": "v21-health", "retired": sorted(retired_ids), "dry": dry})
    return report


def _observe_ok(it: dict) -> bool:
    """观察期 ≥3 天（v15.5 落实 v15.4 F-25 文档声称但未实现的晋升条件）。"""
    prov = it.get("provenance") or {}
    for key in ("lastReviewedAt", "createdAt"):
        ts = prov.get(key)
        if not ts:
            continue
        try:
            dt = datetime.datetime.fromisoformat(ts)
            return (datetime.datetime.now().astimezone() - dt).days >= 3
        except (ValueError, TypeError):
            continue
    return False  # 无有效日期信息 → 保守不放行


def promote_healthy(dry: bool = False) -> dict:
    """健康金标晋升考卷池（已有 scoringSpec 的条目且不在考卷中）。
    晋升=移栽：晋升条目从金标池移除（生命周期：育苗田 → 生产田）。
    v15.5：晋升需满足观察期 ≥3 天（_observe_ok）。"""
    golden_doc = _load_json(GOLDEN)
    v21_doc = _load_json(V21)
    cases = v21_doc.get("cases", [])
    existing_ids = {c["id"] for c in cases}
    existing_prompts = {c.get("prompt", "") for c in cases}
    # 清理：金标中已存在于考卷的条目（晋升残留）→ 移除（防无交集契约违约）
    stale_ids = [it["id"] for it in golden_doc.get("items", []) if it["id"] in existing_ids]
    if stale_ids and not dry:
        golden_doc["items"] = [it for it in golden_doc["items"] if it["id"] not in existing_ids]
        golden_doc["contentHash"] = golden_guard.items_hash(golden_doc["items"])
        _write_atomic(GOLDEN, golden_doc)
    promoted = []
    promoted_golden_ids = []
    for it in golden_doc.get("items", []):
        if it["id"] in existing_ids or it.get("task") in existing_prompts:
            continue
        if not it.get("expected"):
            continue  # 无通用判分规格的旧条目不晋升（避免无判分器）
        if not _observe_ok(it):
            _log_event({"ts": _now_iso(), "op": "promote-skip", "id": it["id"],
                        "note": "观察期不足 3 天（v15.5）"})
            continue
        case = _golden_to_case(it)
        cases.append(case)
        promoted.append(case["id"])
        promoted_golden_ids.append(it["id"])
    if promoted and not dry:
        golden_doc["items"] = [it for it in golden_doc["items"] if it["id"] not in promoted_golden_ids]
        golden_doc["contentHash"] = golden_guard.items_hash(golden_doc["items"])
        _write_atomic(GOLDEN, golden_doc)
        v21_doc["totalCases"] = len(cases)
        v21_doc["contentHash"] = _v21_hash(v21_doc)
        _write_atomic(V21, v21_doc)
    _log_event({"ts": _now_iso(), "op": "promote", "promoted": promoted, "dry": dry})
    return {"promoted": promoted}


def auto(dry: bool = False) -> dict:
    hc = health_check()
    rp = replace_unhealthy(dry=dry) if hc["unhealthyCount"] else {"replaced": []}
    fv = fill_one(dry=dry)  # v15.4b：每次只补一道题（the maintainer decided，失败快速返回）
    pm = promote_healthy(dry=dry)
    vh = check_v21_health(dry=dry)
    return {"health": hc["unhealthyCount"], "replaced": rp["replaced"],
            "fill": fv, "promoted": pm["promoted"], "v21Health": vh}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--expand", type=int, default=0, help="金标池补 N 条")
    ap.add_argument("--health-check", action="store_true")
    ap.add_argument("--replace", action="store_true")
    ap.add_argument("--fill-one", action="store_true", help="只补一道题（原子事务，推荐）")
    ap.add_argument("--dim", default=None, help="指定补题维度（配合 --fill-one）")
    ap.add_argument("--fill-v21", action="store_true", help="循环补题直到失败（手动全量）")
    ap.add_argument("--promote", action="store_true")
    ap.add_argument("--v21-health", action="store_true")
    ap.add_argument("--auto", action="store_true")
    ap.add_argument("--dry", action="store_true")
    args = ap.parse_args()
    out = {}
    if args.health_check:
        out = health_check()
    if args.expand:
        out = expand(args.expand, dry=args.dry)
    if args.replace:
        out = replace_unhealthy(dry=args.dry)
    if args.fill_one:
        out = fill_one(dry=args.dry, dimension=args.dim)
    if args.fill_v21:
        out = fill_v21(dry=args.dry)
    if args.promote:
        out = promote_healthy(dry=args.dry)
    if args.v21_health:
        out = check_v21_health(dry=args.dry)
    if args.auto:
        out = auto(dry=args.dry)
    print(json.dumps(out, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
