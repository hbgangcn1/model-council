"""judge 资格考试（v15.5-E：金标升级评卷考场后，judge 的选择依据 = 评卷考试分）。

考核对象：DSH 池内模型 × 低思考档（评卷用低档足够，成本可控）。
考核方式：对金标集每条「好答案 + badAnswers」按 rubric 打分。
指标：
- 标定准确度 = mean(|judge 分 − expectedScore|)，越小越好；
- 区分度 = mean(好答案分) − mean(坏答案分)，越大越好；
- 稳定性 = 与上次考核同题打分的均差（judge-profiles.json 留存对比）。
成绩写入 judge-profiles.json（judge 档案）；换任评估由调度侧读档案决定。

用法：python benchmark/judge_qualify.py [--dry] [--limit N]
"""
import argparse
import json
import sys
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent  # council/
sys.path.insert(0, str(BASE))
sys.path.insert(0, str(BASE / "orchestrator"))

from orchestrator import stream_llm, config_loader  # noqa: E402
import pool as pool_mod  # noqa: E402
import bridge as bridge_mod  # noqa: E402
from benchmark import golden_guard  # noqa: E402

GOLDEN = BASE / "benchmark" / "golden" / "golden-set.json"
PROFILES = BASE / "judge-profiles.json"
EVENTS = BASE / "benchmark" / "judge-qualify-events.jsonl"

# 考核档位：低思考档（评卷不需要高思考；minimal/off 打分能力不足，选 low/medium）
QUALIFY_LEVELS = {"deepseek-v4-pro": "low", "deepseek-v4-flash": "low",
                  "MiniMax-M3": "low"}

JUDGE_PROMPT = """你是 benchmark 评分员。按 rubric 给下面的「待评答案」打分。
只输出 JSON（不要其他文字）：{"score": 0-10 的一位小数, "rationale": "一句理由"}

任务：{task}

待评答案：
{answer}

评分 rubric：
{rubric}"""


def _extract_score(text: str):
    import re
    m = re.search(r"\{.*\}", text or "", re.S)
    if not m:
        return None
    try:
        obj = json.loads(m.group(0))
        s = obj.get("score")
        return float(s) if isinstance(s, (int, float)) else None
    except (json.JSONDecodeError, ValueError):
        return None


def _load_profiles() -> dict:
    if PROFILES.exists():
        return json.loads(PROFILES.read_text(encoding="utf-8"))
    return {"schemaVersion": 1, "judges": {}}


def _save_profiles(doc: dict):
    tmp = PROFILES.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(doc, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(PROFILES)


def _log_event(ev: dict):
    try:
        with EVENTS.open("a", encoding="utf-8") as f:
            f.write(json.dumps(ev, ensure_ascii=False) + "\n")
    except OSError:
        pass


def qualify_one(model: str, level: str, items: list, limit: int = 0) -> dict:
    """对金标集打分（好答案 + 坏答案）。返回档案条目。"""
    recs = []
    sample_items = items[:limit] if limit else items
    for it in sample_items:
        # 好答案
        try:
            text, meta = stream_llm.call_stream(
                model, level, JUDGE_PROMPT.format(task=it["task"], answer=it["answer"],
                                                  rubric=it.get("rubric", "")),
                config_loader.max_tokens_for_model(model))
            s = _extract_score(text)
            recs.append({"id": it["id"], "kind": "good", "expected": it.get("expectedScore", 9.5),
                         "score": s, "ok": s is not None and not meta.get("timeout_kind")})
        except Exception as e:
            recs.append({"id": it["id"], "kind": "good", "expected": it.get("expectedScore", 9.5),
                         "score": None, "ok": False, "error": str(e)[:150]})
        # 坏答案
        for b in it.get("badAnswers", []):
            try:
                text, meta = stream_llm.call_stream(
                    model, level, JUDGE_PROMPT.format(task=it["task"], answer=b["text"],
                                                      rubric=it.get("rubric", "")),
                    config_loader.max_tokens_for_model(model))
                s = _extract_score(text)
                recs.append({"id": it["id"], "kind": "bad", "label": b.get("label"),
                             "expected": b.get("expectedScore", 1.0),
                             "score": s, "ok": s is not None and not meta.get("timeout_kind")})
            except Exception as e:
                recs.append({"id": it["id"], "kind": "bad", "label": b.get("label"),
                             "expected": b.get("expectedScore", 1.0),
                             "score": None, "ok": False, "error": str(e)[:150]})
    good = [r for r in recs if r["kind"] == "good" and r["ok"]]
    bad = [r for r in recs if r["kind"] == "bad" and r["ok"]]
    calib = (sum(abs(r["score"] - r["expected"]) for r in good + bad) / len(good + bad)) \
        if (good + bad) else None
    disc = (sum(r["score"] for r in good) / len(good) - sum(r["score"] for r in bad) / len(bad)) \
        if good and bad else None
    return {"model": model, "level": level, "calibrationError": calib,
            "discrimination": disc, "nGood": len(good), "nBad": len(bad),
            "checkedAt": config_loader.now_shanghai().isoformat(), "recs": recs}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry", action="store_true")
    ap.add_argument("--limit", type=int, default=0, help="只考前 N 条（测试用）")
    args = ap.parse_args()
    golden_doc = json.loads(GOLDEN.read_text(encoding="utf-8"))
    items = golden_doc.get("items", [])
    doc = _load_profiles()
    out = []
    for model in pool_mod.members():
        level = QUALIFY_LEVELS.get(model, "low")
        try:
            bridge_mod.model_entry(model)  # fail-loud：不在 host catalog的模型跳过
        except ValueError:
            _log_event({"ts": config_loader.now_shanghai().isoformat(),
                        "op": "qualify-skip", "model": model, "reason": "不在 host-side tier-bridge"})
            continue
        if args.dry:
            entry = {"model": model, "level": level, "calibrationError": None,
                     "discrimination": None, "nGood": 0, "nBad": 0,
                     "checkedAt": config_loader.now_shanghai().isoformat(),
                     "recs": [], "dry": True}
        else:
            entry = qualify_one(model, level, items, args.limit)
        doc.setdefault("judges", {})[model] = entry
        out.append(entry)
        print(f"[QUALIFY] {model}@{level}: calib={entry.get('calibrationError')}, "
              f"disc={entry.get('discrimination')}, n={entry.get('nGood')}+{entry.get('nBad')}")
    if not args.dry:
        _save_profiles(doc)
    _log_event({"ts": config_loader.now_shanghai().isoformat(),
                "op": "qualify", "dry": args.dry, "judges": len(out)})
    print("✅ judge 档案：" + str(PROFILES))


if __name__ == "__main__":
    main()
