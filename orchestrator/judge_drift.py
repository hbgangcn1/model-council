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
import threading
import time
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
PROGRESS_OUT = BASE / "judge-progress.json"  # 跑一题写一次，供控制台进度卡轮询

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


def _preflight_quota(*models) -> None:
    """开工前额度预检：MiniMax 系 judge 且 5h 快照为 0% → 直接报额度用尽。

    读免费的本地快照（60 秒缓存，不花钱）；快照缺失/读取失败一律放行，
    不让预检本身误杀正常运行。跑中途见底由 score_items 的连续 quota 熔断接管。
    """
    if not any((m or "").startswith("MiniMax") for m in models if m):
        return
    try:
        try:
            from . import query_balance as _qb
        except ImportError:
            import query_balance as _qb  # type: ignore  # 直接脚本模式兜底
        snap = _qb.query()
        pct = (snap.get("ok") or {}).get("minimax-cn:5h")
        if pct is not None and float(pct) <= 0:
            raise RuntimeError(
                "QUOTA_EXHAUSTED: MiniMax 5h 窗口剩余额度为 0%（开工预检），"
                "本轮自评跳过，不重试")
    except RuntimeError:
        raise
    except Exception:
        pass  # fail-open


def _write_progress(state: dict):
    """进度文件原子写（best-effort，写失败不影响评分本身）。"""
    try:
        tmp = PROGRESS_OUT.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(state, ensure_ascii=False), encoding="utf-8")
        tmp.replace(PROGRESS_OUT)
    except OSError:
        pass


def _is_quota_error(rec: dict) -> bool:
    return "QUOTA_EXHAUSTED" in str((rec or {}).get("error", ""))


def _write_quota_abort(judge_model, judge_thinking, golden, scores) -> dict:
    """额度用尽中止：部分结果照写、标 quotaExhausted，不写漂移结论。"""
    out = {"initializedBaseline": False, "generatedAt": now_shanghai().isoformat(),
           "judge": f"{judge_model}@{judge_thinking}",
           "promptHash": _prompt_hash(golden), "goldenVersion": golden.get("version"),
           "baselineMean": None, "currentMean": None, "drift": None,
           "driftLevel": "ok", "alerted": False,
           "quotaExhausted": True, "stalled": False,
           "scores": scores, "note": "额度用尽中止：有效打分 0 题，不写漂移结论"}
    tmp = DRIFT_OUT.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(out, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(DRIFT_OUT)
    _write_event({"ts": out["generatedAt"], "event": "quota_exhausted",
                  "judge": out["judge"]})
    return out


def score_items(items: list, judge_model: str, judge_thinking: str,
                max_workers: int = 3, max_runtime_s: float = 540.0,
                stage: str = "main", progress_cb=None,
                pass_timeout_s: float = 120.0) -> list:
    """v15.8 按 pass 跑 + 活性门：题目一次提交，并行收结果；跑一题写一次进度。

    结束条件（按优先级）：
    1. 全部判完 → 正常返回；
    2. 连续 3 题 QUOTA_EXHAUSTED → 剩题标 quota_aborted，调用方整轮 fast-abort；
    3. 连续两 pass 零进展 → 剩题标 stalled，诚实返回部分结果；
    4. max_runtime_s 绝对 backstop（只防真死锁，正常应由 1/3 先触发）。
    旧签名兼容：新增全是关键字可选参数。
    """
    from concurrent.futures import (ThreadPoolExecutor, as_completed,
                                    TimeoutError as FuturesTimeout)
    label = f"{judge_model}@{judge_thinking}"
    total = len(items)
    if not items:
        return []
    max_tok = max_tokens_for_model(judge_model)
    def _score_one(it):
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
        return rec
    out = [None] * total
    t_start = time.monotonic()
    done_n = ok_n = quota_n = 0
    consec_zero = 0   # 连续零进展 pass 数
    consec_quota = 0  # 连续额度错个数

    def _report(extra=None):
        if progress_cb is None:
            return
        try:
            progress_cb(stage, label, done_n, total, ok_n, quota_n, extra)
        except Exception:
            pass

    ex = ThreadPoolExecutor(max_workers=max_workers)
    try:
        futs = [ex.submit(_score_one, it) for it in items]
        pending = set(futs)
        idx_of = {fut: i for i, fut in enumerate(futs)}
        _report()
        while pending:
            if time.monotonic() - t_start >= max_runtime_s:
                break  # 绝对 backstop
            pass_budget = max(1.0, min(pass_timeout_s,
                                       max_runtime_s - (time.monotonic() - t_start)))
            made_progress = False
            try:
                for fut in as_completed(list(pending), timeout=pass_budget):
                    pending.discard(fut)
                    idx = idx_of[fut]
                    try:
                        rec = fut.result()
                    except Exception as e:
                        rec = {"id": items[idx]["id"],
                               "dimension": items[idx].get("dimension"),
                               "ok": False,
                               "error": f"executor:{type(e).__name__}"}
                    if out[idx] is None:  # 迟到结果不覆盖已标记
                        out[idx] = rec
                        done_n += 1
                        made_progress = True
                        if rec.get("ok"):
                            ok_n += 1
                        if _is_quota_error(rec):
                            quota_n += 1
                            consec_quota += 1
                        else:
                            consec_quota = 0
                        _report()
                        if consec_quota >= 3:
                            break  # 额度熔断：剩题直接标，不再烧钱
            except FuturesTimeout:
                pass  # 本 pass 零收获，走下面的活性判断
            if consec_quota >= 3:
                break
            if made_progress:
                consec_zero = 0
            else:
                consec_zero += 1
                _report()
                if consec_zero >= 2:
                    break  # 活性门：连续两 pass 零进展，诚实退出
    finally:
        for fut in list(pending):
            fut.cancel()
        ex.shutdown(wait=False, cancel_futures=True)
    quota_aborted = consec_quota >= 3
    budget_hit = (time.monotonic() - t_start) >= max_runtime_s
    stalled = bool(not quota_aborted and any(r is None for r in out))
    for i in range(total):
        if out[i] is None:
            if quota_aborted:
                out[i] = {"id": items[i]["id"],
                          "dimension": items[i].get("dimension"),
                          "ok": False, "error": "quota_aborted"}
            elif budget_hit:
                out[i] = {"id": items[i]["id"],
                          "dimension": items[i].get("dimension"),
                          "ok": False,
                          "error": f"timeout_after_{int(max_runtime_s)}s"}
            else:
                out[i] = {"id": items[i]["id"],
                          "dimension": items[i].get("dimension"),
                          "ok": False, "error": "stalled_no_progress",
                          "stalled": True}
    _report({"stalled": stalled, "quotaAborted": quota_aborted})
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


def run(init: bool = False, dry: bool = False, max_runtime_s: float = 540.0,
        max_workers: int = 3) -> dict:
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
    # v15.8 活性门替代固定配额：还在跑就不杀。每阶段拿 min(上限, 全局剩余)，
    # 上限只是防真死锁的 backstop（总和 1200s < 宿主 exec 1500s，留落盘余量）；
    # 正常结束靠“判完”或“连续两 pass 零进展”，进度经 progress_cb 落文件供控制台轮询。
    _preflight_quota(judge_model, j2_model)
    t_start = time.monotonic()
    _prog_lock = threading.Lock()
    def _cb(stage, label, done, total, ok, quota, extra=None):
        st = {"stage": stage, "judge": label, "done": done, "total": total,
              "ok": ok, "quotaErrors": quota,
              "updatedAt": now_shanghai().isoformat()}
        if extra:
            st.update(extra)
        with _prog_lock:
            _write_progress(st)
    def _remaining(default: float) -> float:
        return max(0.0, min(float(default), max_runtime_s - (time.monotonic() - t_start)))
    scores = score_items(items, judge_model, judge_thinking,
                         max_workers=max_workers, max_runtime_s=_remaining(750.0),
                         stage="main", progress_cb=_cb)
    if not any(s.get("ok") for s in scores):
        # 主 judge 一题没判出来：额度见底或全挂，写部分结果 + 明确错误让 nightly 跳过
        _write_quota_abort(judge_model, judge_thinking, golden, scores)
        raise RuntimeError("QUOTA_EXHAUSTED: 主 judge 有效打分 0 题，本轮自评中止，不重试")
    # v15.3：init 重建基线只跑主 judge——crossJudge（scores2）是日常自评的交叉验证，
    # 基线只需主 judge 分布，白跑 36 条第二 judge 纯属浪费（金标扩到 36 条后尤其明显）
    scores2 = score_items(items, j2_model, j2_thinking,
                          max_workers=max_workers, max_runtime_s=_remaining(300.0),
                          stage="cross", progress_cb=_cb) if (j2_model and not init) else None
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
        hs = score_items(holdout_items, judge_model, judge_thinking,
                         max_workers=max_workers, max_runtime_s=_remaining(150.0),
                         stage="holdout", progress_cb=_cb)
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
    def _timeout_n(rows):
        return sum(1 for s in (rows or []) if str((s or {}).get("error", "")).startswith("timeout_after"))
    def _quota_n(rows):
        return sum(1 for s in (rows or []) if _is_quota_error(s))
    out = {"initializedBaseline": False, "generatedAt": now_shanghai().isoformat(),
           "judge": f"{judge_model}@{judge_thinking}",
           "promptHash": ph, "goldenVersion": golden.get("version"),
           "baselineMean": baseline.get("mean"), "currentMean": mean,
           "drift": drift, "driftLevel": drift_level,
           "warnDrift": warn_drift, "alertThreshold": threshold, "alerted": alerted,
           "holdout": holdout, "crossJudge": cross, "dimDrift": dim_drift,
           "scores": scores, "baselineCreatedAt": baseline.get("createdAt"),
           "quotaExhausted": bool(_quota_n(scores) or _quota_n(scores2)),
            "stalled": any((s or {}).get("stalled") for s in (scores or [])),
           "quotaErrors": {"main": _quota_n(scores), "cross": _quota_n(scores2),
                            "holdout": _quota_n(hs if holdout_items else [])},
           "timeouts": {"main": _timeout_n(scores), "cross": _timeout_n(scores2),
                        "holdout": _timeout_n(hs if holdout_items else [])}}
    try:
        _cb("done", f"{judge_model}@{judge_thinking}", len(scores), len(scores),
            sum(1 for s in scores if s.get("ok")), _quota_n(scores),
            {"alerted": alerted})
    except Exception:
        pass
    tmp = DRIFT_OUT.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(out, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(DRIFT_OUT)
    if alerted:
        _write_event({"ts": out["generatedAt"], "drift": drift,
                      "threshold": threshold, "judge": out["judge"],
                      "crossJudge": cross, "holdout": holdout})
    return out


if __name__ == "__main__":
    args = list(sys.argv[1:])
    args_set = set(args)
    init = "--init-baseline" in args_set
    dry = "--dry" in args_set
    # --max-runtime 是三阶段共享的总量预算（默认 1200s：主≤750/交叉≤300/holdout≤150 backstop）。
    # 正常结束不靠它（靠判完或活性门），它只防真死锁；必须小于宿主 exec 超时（1500s）。
    max_runtime_s = 1200.0
    max_workers = 3
    if "--max-runtime" in args:
        max_runtime_s = float(args[args.index("--max-runtime") + 1])
    if "--max-workers" in args:
        max_workers = int(args[args.index("--max-workers") + 1])
    try:
        result = run(init=init, dry=dry, max_runtime_s=max_runtime_s,
                     max_workers=max_workers)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        sys.exit(2 if result.get("alerted") else 0)
    except Exception as e:
        print(json.dumps({"error": str(e)[:300]}, ensure_ascii=False, indent=2))
        sys.exit(1)
