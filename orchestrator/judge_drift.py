"""judge 漂移监控（评审报告 M 项）：金标集每日自评，漂移超阈值告警。

原理：金标集 = 固定「任务 + 固定答案 + rubric」（benchmark/golden/golden-set.json）。
每次运行让 judge 模型（默认 MiniMax-M3@medium）按 rubric 给固定答案打分——
答案不变，分数变化只能归因于 judge 漂移（系统提示漂移/模型升级/温度漂移）。
与基线（judge-baseline.json，首次 --init-baseline 写入）比较：
|当前均分 − 基线均分| > alertThreshold → 告警事件落 judge-drift-events.jsonl，
退出码 2（宿主据此通知），并在 judge-drift.json 标记 alerted。

v15.2（P1-2）：
- 基线记录 promptHash（JUDGE_PROMPT + rubric 哈希）与 goldenVersion；prompt/rubric 变更
  → 强制重建基线并写事件（此前静默沿用旧基线，漂移被掩盖）；
- 双 judge 交叉验证：judge2（默认 deepseek-v4-flash@low）同题打分，均分差超 crossJudgeAlert → 告警；
- holdout 子集：golden 条目带 holdout=true 时单独计算漂移（防泄漏敏感子集）。

用法：
  python orchestrator/judge_drift.py                 # 自评 + 对比基线（无基线则自动初始化）
  python orchestrator/judge_drift.py --init-baseline # 强制重建基线
  python orchestrator/judge_drift.py --dry           # 只算计划不调 API
退出码：0=正常 / 1=执行失败（API 异常等）/ 2=漂移告警。
"""
import hashlib
import json
import re
import sys
from pathlib import Path

try:
    from .config_loader import now_shanghai, max_tokens_for_model, thinking_param
    from .stream_llm import call_stream
    from . import params as params_mod
except ImportError:
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    from config_loader import now_shanghai, max_tokens_for_model, thinking_param
    from stream_llm import call_stream
    import params as params_mod  # noqa: E402

BASE = Path(__file__).resolve().parent.parent  # council/
GOLDEN = BASE / "benchmark" / "golden" / "golden-set.json"
BASELINE = BASE / "benchmark" / "golden" / "judge-baseline.json"
DRIFT_OUT = BASE / "judge-drift.json"
DRIFT_EVENTS = BASE / "judge-drift-events.jsonl"

JUDGE_PROMPT = """你是 benchmark 评分员。按 rubric 给下面的「固定答案」打分。
只输出 JSON（不要其他文字）：{{"score": 0-10 的一位小数, "rationale": "一句理由"}}

任务：{task}

固定答案：
{answer}

评分 rubric：
{rubric}"""


def _extract_score(text: str):
    m = re.search(r"\{.*\}", text, re.S)
    if not m:
        return None
    try:
        obj = json.loads(m.group(0))
        s = obj.get("score")
        return float(s) if isinstance(s, (int, float)) else None
    except (json.JSONDecodeError, ValueError):
        return None


def _drift_params() -> dict:
    return params_mod.load().get("judgeDrift", params_mod.DEFAULTS["judgeDrift"])


def _judge_model() -> tuple:
    dp = _drift_params()
    return dp.get("judgeModel", "MiniMax-M3"), dp.get("judgeThinking", "medium")


def score_items(items: list, judge_model: str, judge_thinking: str) -> list:
    """逐题调用 judge。返回 [{id, score, rationale, ok, error}]。"""
    out = []
    max_tok = max_tokens_for_model(judge_model)  # v15.5：模型上限来自 host-side tier-bridge
    for it in items:
        rec = {"id": it["id"], "dimension": it.get("dimension"), "ok": False}
        try:
            text, meta = call_stream(
                judge_model, judge_thinking,
                JUDGE_PROMPT.format(task=it["task"], answer=it["answer"],
                                    rubric=it.get("rubric", "")),
                max_tok)
            if meta.get("timeout_kind") or meta.get("finish_reason") == "error" or not text.strip():
                rec["error"] = meta.get("timeout_kind") or meta.get("finish_reason") or "empty"
            else:
                s = _extract_score(text)
                if s is None:
                    rec["error"] = "score_unparsable"
                else:
                    rec["score"] = max(0.0, min(10.0, s))
                    rec["ok"] = True
        except Exception as e:
            rec["error"] = str(e)[:200]
        out.append(rec)
    return out


def _load_golden() -> dict:
    return json.loads(GOLDEN.read_text(encoding="utf-8"))


def _load_baseline() -> dict:
    if BASELINE.exists():
        try:
            return json.loads(BASELINE.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            pass
    return {}


def _mean(items: list) -> float:
    vals = [i["score"] for i in items if i.get("ok")]
    return round(sum(vals) / len(vals), 2) if vals else None


def drift_level_for(drift, warn, alert, critical) -> str:
    """v15.5b（元评审 F5）：漂移三档 level 纯函数——warn < alert < critical。"""
    if drift is None:
        return "ok"
    da = abs(drift)
    if da >= critical:
        return "critical"
    if da >= alert:
        return "alert"
    if da >= warn:
        return "warn"
    return "ok"


def compute_drift(current_mean, baseline_mean, threshold: float) -> dict:
    """纯函数：漂移计算与告警判定（可离线测试）。"""
    if current_mean is None or baseline_mean is None:
        return {"drift": None, "alerted": False}
    drift = round(current_mean - baseline_mean, 2)
    return {"drift": drift, "alerted": abs(drift) > threshold}


def _prompt_hash(golden: dict) -> str:
    """P1-2/P2-3：JUDGE_PROMPT + 各题 rubric + answer 的哈希。
    prompt/rubric 或固定答案任一变更 → 基线失效重建（答案变分数必然变，基线必须跟着变）。"""
    content = JUDGE_PROMPT + json.dumps(
        [{"id": it.get("id"), "rubric": it.get("rubric"), "answer": it.get("answer")}
         for it in golden.get("items", [])],
        ensure_ascii=False, sort_keys=True)
    return hashlib.sha256(content.encode("utf-8")).hexdigest()[:16]


def _write_event(ev: dict):
    try:
        with DRIFT_EVENTS.open("a", encoding="utf-8") as f:
            f.write(json.dumps(ev, ensure_ascii=False) + "\n")
    except OSError:
        pass


def init_baseline(items: list, scores: list, prompt_hash: str = None,
                  golden_version=None) -> dict:
    ok = [s for s in scores if s.get("ok")]
    if len(ok) < int(_drift_params().get("baselineMinItems", 3)):
        raise RuntimeError(f"有效打分不足 {_drift_params().get('baselineMinItems', 3)} 题，不建基线")
    base = {"createdAt": now_shanghai().isoformat(),
            "judge": f"{_judge_model()[0]}@{_judge_model()[1]}",
            "promptHash": prompt_hash,
            "goldenVersion": golden_version,
            "perItem": {s["id"]: s["score"] for s in ok},
            "mean": _mean(ok)}
    tmp = BASELINE.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(base, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(BASELINE)
    return base


def run(init: bool = False, dry: bool = False) -> dict:
    dp = _drift_params()
    golden = _load_golden()
    items = golden.get("items") or []
    judge_model, judge_thinking = _judge_model()
    j2_model = dp.get("judge2Model") or None
    j2_thinking = dp.get("judge2Thinking") or "low"
    if dry:
        return {"dry": True, "items": len(items),
                "judge": f"{judge_model}@{judge_thinking}",
                "judge2": (f"{j2_model}@{j2_thinking}" if j2_model else None)}
    scores = score_items(items, judge_model, judge_thinking)
    # v15.3：init 重建基线只跑主 judge——crossJudge（scores2）是日常自评的交叉验证，
    # 基线只需主 judge 分布，白跑 36 条第二 judge 纯属浪费（金标扩到 36 条后尤其明显）
    scores2 = score_items(items, j2_model, j2_thinking) if (j2_model and not init) else None
    baseline = _load_baseline()
    ph = _prompt_hash(golden)
    if init or not baseline or baseline.get("promptHash") != ph:
        reason = "forced" if init else ("prompt_or_rubric_changed" if baseline else "baseline_missing")
        base = init_baseline(items, scores, prompt_hash=ph,
                             golden_version=golden.get("version"))
        out = {"initializedBaseline": True, "baselineMean": base["mean"],
               "scores": scores, "generatedAt": now_shanghai().isoformat(),
               "judge": f"{judge_model}@{judge_thinking}",
               "promptHash": ph, "goldenVersion": golden.get("version")}
        tmp = DRIFT_OUT.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(out, ensure_ascii=False, indent=2), encoding="utf-8")
        tmp.replace(DRIFT_OUT)
        _write_event({"ts": out["generatedAt"], "event": "baseline_initialized",
                      "reason": reason, "judge": out["judge"], "mean": base["mean"],
                      "promptHash": ph})
        return out
    mean = _mean(scores)
    threshold = float(dp.get("alertThreshold", 1.0))
    verdict = compute_drift(mean, baseline.get("mean"), threshold)
    drift = verdict["drift"]
    # P1-2：holdout 子集独立漂移（golden 条目 holdout=true 优先、更防泄漏）
    holdout_items = [it for it in items if it.get("holdout")]
    holdout = None
    if holdout_items:
        hs = score_items(holdout_items, judge_model, judge_thinking)
        hm = _mean(hs)
        base_holdout = {s["id"]: s["score"] for s in hs if s.get("ok")}
        base_map = baseline.get("perItem") or {}
        base_vals = [base_map[i["id"]] for i in holdout_items
                     if base_map.get(i["id"]) is not None]
        hb = round(sum(base_vals) / len(base_vals), 2) if base_vals else None
        if hm is not None and hb is not None:
            holdout = {"n": len(hs), "mean": hm, "baselineMean": hb,
                       "drift": round(hm - hb, 2)}
    # P1-2：双 judge 交叉验证
    cross = None
    if scores2 is not None:
        mean2 = _mean(scores2)
        if mean is not None and mean2 is not None:
            cross = {"judge2": f"{j2_model}@{j2_thinking}", "mean2": mean2,
                     "delta": round(mean - mean2, 2)}
            if abs(cross["delta"]) > float(dp.get("crossJudgeAlert", 1.0)):
                cross["alerted"] = True
    # v15.5 分维度漂移：总分正常可能掩盖分维度漂移（任一维度超阈即告警）
    dim_drift = {}
    base_map = baseline.get("perItem") or {}
    for dim in sorted({it.get("dimension") for it in items if it.get("dimension")}):
        dim_ids = [it["id"] for it in items if it.get("dimension") == dim]
        cur_vals = [s["score"] for s in scores if s.get("ok") and s["id"] in dim_ids]
        base_vals = [base_map[i] for i in dim_ids if base_map.get(i) is not None]
        if cur_vals and base_vals:
            cm = sum(cur_vals) / len(cur_vals)
            bm = sum(base_vals) / len(base_vals)
            d = round(cm - bm, 2)
            dim_drift[dim] = {"drift": d, "alerted": abs(d) > threshold}
    dim_alerted = any(v["alerted"] for v in dim_drift.values())
    alerted = verdict["alerted"] or dim_alerted or bool(cross and cross.get("alerted"))
    # v15.5b（元评审 F5）：漂移三档 level 固化（warn < alert < critical，阈值 params 化）
    warn_drift = float(dp.get("warnDrift", 0.3))
    drift_level = drift_level_for(drift, warn_drift,
                                  float(dp.get("pauseWriteDrift", 0.5)), threshold)
    out = {"initializedBaseline": False, "generatedAt": now_shanghai().isoformat(),
           "judge": f"{judge_model}@{judge_thinking}",
           "promptHash": ph, "goldenVersion": golden.get("version"),
           "baselineMean": baseline.get("mean"), "currentMean": mean,
           "drift": drift, "driftLevel": drift_level,
           "warnDrift": warn_drift, "alertThreshold": threshold, "alerted": alerted,
           "holdout": holdout, "crossJudge": cross, "dimDrift": dim_drift,
           "scores": scores, "baselineCreatedAt": baseline.get("createdAt")}
    tmp = DRIFT_OUT.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(out, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(DRIFT_OUT)
    if alerted:
        _write_event({"ts": out["generatedAt"], "drift": drift,
                      "threshold": threshold, "judge": out["judge"],
                      "crossJudge": cross, "holdout": holdout})
    return out


if __name__ == "__main__":
    args = set(sys.argv[1:])
    try:
        result = run(init="--init-baseline" in args, dry="--dry" in args)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        sys.exit(2 if result.get("alerted") else 0)
    except Exception as e:
        print(json.dumps({"error": str(e)[:300]}, ensure_ascii=False, indent=2))
        sys.exit(1)
