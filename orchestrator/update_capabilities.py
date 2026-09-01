"""运行期反馈合成器（§3.5）：runtime-feedback.jsonl → 贝叶斯融合更新 capabilities.json。

v15.1（评审报告 H 项加固）：
- 文件锁：caps_guard/file_lock 防并发 council 互相覆盖；
- 写前校验：score ∈ [0,10]、revision 单调递增，坏数据拒绝落盘；
- sourceRunIds：runtimeFeedback 记录本次融合实际用到的 run_id 清单（可回溯）；
- 最小改动阈值修正：|Δ|<minScoreChange 只更新样本数不写分（防噪声，原实现写反了）；
- 失败向上抛异常（调用方写告警，不再静默吞）；
- 无有效反馈时 skipped（不空转 revision）。

v15.2（2026-08-24 元评审 P0-5/P1-2/P0-4 落地）：
- P0-5 同源隔离：feedback 行带 scoredBy；requireHeteroScorer=true 时拒绝 scoredBy==model 的行；
- P0-5 人工审批：council 收尾默认只写 pending diff（pending_diff()），
  `--apply` 校验 baseHash 后才落盘；autoApply=true 才走 update() 直接落盘；
- P1-2 漂移门禁：judge-drift.json |drift| ≥ pauseWriteDrift → 拒绝写档案；
- P0-4 客观遥测：update_runtime_telemetry() 每 run 收尾自动回填 runtime/cost 字段
  （latencyP50Ms/avgVerifyScore/successRate/avgInputTokens…，不改能力分、不 bump revision），
  复活 selector 的延迟项。

用法：
  python orchestrator/update_capabilities.py          # 执行融合（直接落盘，手动调用）
  python orchestrator/update_capabilities.py --apply  # 审批 pending diff 后落盘
  python orchestrator/update_capabilities.py --dry    # 只算不落盘
"""
import hashlib
import json
import math
import sys
from pathlib import Path

try:  # 包内导入；直接执行时退回顶层导入（脚本目录已在 sys.path[0]）
    from .caps_guard import validate_or_raise
    from .config_loader import now_shanghai
    from .file_lock import file_lock
    from . import params as params_mod
except ImportError:
    from caps_guard import validate_or_raise  # type: ignore
    from config_loader import now_shanghai  # type: ignore
    from file_lock import file_lock  # type: ignore
    import params as params_mod  # type: ignore

BASE = Path(__file__).resolve().parent.parent  # council/
FEEDBACK = BASE / "evals" / "runtime-feedback.jsonl"
CAPS = BASE / "capabilities.json"
PENDING = BASE / "evals" / "pending-runtime-diff.json"
SAMPLE_KNEE = 20          # 样本拐点（默认值；params.feedback.sampleKnee 可覆盖）
MIN_FLIP_RUNS = 10        # 翻排名门槛（默认值；params.feedback.minFlipRuns 可覆盖）
LOCK_TIMEOUT_S = 30
LOCK_STALE_S = 120


def _fb_params() -> dict:
    return params_mod.load().get("feedback", params_mod.DEFAULTS["feedback"])


def _caps_hash(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _group_zscore(rows):
    """按 run_id 分组做 z-score（同一轮内相对比较）。"""
    by_run = {}
    for r in rows:
        by_run.setdefault(r["run_id"], []).append(r)
    for run_id, group in by_run.items():
        scores = [g["verifierScore"] for g in group if g.get("verifierScore") is not None]
        if len(scores) < 2:
            for g in group:
                g["z"] = 0.0
            continue
        mean = sum(scores) / len(scores)
        std = math.sqrt(sum((s - mean) ** 2 for s in scores) / len(scores)) or 1.0
        for g in group:
            g["z"] = ((g.get("verifierScore") or mean) - mean) / std
    return rows


def _feedback_to_runtime_scores():
    """→ (runtime_scores, contributing_run_ids, rejected_self_scored)。runtime_scores: {cid: {dim: {zSum, n}}}。
    P0-5：requireHeteroScorer=true 时拒绝 scoredBy==model 的同源行（自评自选循环切断）。"""
    if not FEEDBACK.exists():
        return {}, set(), 0
    rows = []
    for line in FEEDBACK.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            rows.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    valid = [r for r in rows if r.get("success") and not r.get("hardGateHit")
             and not r.get("reworkTriggered") and r.get("verifierScore") is not None]
    rejected_self = 0
    if bool(_fb_params().get("requireHeteroScorer", True)):
        kept = []
        for r in valid:
            scored_by = r.get("scoredBy")
            model = r.get("model")
            if scored_by and model and scored_by == model:
                rejected_self += 1
                continue
            kept.append(r)
        valid = kept
    valid = _group_zscore(valid)
    contributing = {r["run_id"] for r in valid if r.get("run_id")}
    # 维度归因：按 taskVector 分解 z 分
    model_dim_scores = {}
    model_dim_counts = {}
    for r in valid:
        model = r["model"]
        thinking = r.get("thinking", "off")
        cid = f"{model}__{thinking}"
        tv = r.get("taskVector", {})
        if not tv:
            continue
        for d, w in tv.items():
            model_dim_scores.setdefault(cid, {}).setdefault(d, []).append(r["z"] * w)
            model_dim_counts.setdefault(cid, {}).setdefault(d, 0)
            model_dim_counts[cid][d] += 1
    runtime = {}
    for cid, dims in model_dim_scores.items():
        runtime[cid] = {d: {"zSum": sum(v), "n": model_dim_counts[cid][d]} for d, v in dims.items()}
    return runtime, contributing, rejected_self


def _load_feedback_rows():
    if not FEEDBACK.exists():
        return []
    rows = []
    for line in FEEDBACK.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            rows.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return rows


def _merge_avg(prev, prev_n, vals, ndigits=2):
    """与历史样本做加权平均（新样本权重上限 0.5，避免单次长文带偏）。"""
    if not vals:
        return prev
    new_avg = sum(vals) / len(vals)
    if prev is None or prev_n == 0:
        return round(new_avg, ndigits)
    w = min(0.5, len(vals) / (prev_n + len(vals)))
    return round(w * new_avg + (1 - w) * prev, ndigits)


def plan_runtime_telemetry(caps: dict, rows: list) -> dict:
    """P0-4：客观遥测回填（不改能力分、不 bump revision）。
    runtime: {latencyP50Ms, avgVerifyScore, successRate, samples}
    cost: {avgInputTokens, avgOutputTokens, costPerCallCny}。"""
    new_caps = json.loads(json.dumps(caps))
    by_model = {}
    for r in rows:
        if not r.get("success") or not r.get("model"):
            continue
        cid = f"{r['model']}__{r.get('thinking', 'off')}"
        by_model.setdefault(cid, []).append(r)
    fx = 1.0
    try:
        from . import selector
        fx = selector.load_fx_rate().get("usdToCny") or 1.0
    except Exception:
        pass
    for cid, rs in by_model.items():
        model = new_caps.get("models", {}).get(cid)
        if not model:
            continue
        rt = model.setdefault("runtime", {})
        prev_n = int(rt.get("samples") or 0)
        lats = sorted(r.get("latency_ms") for r in rs if r.get("latency_ms"))
        scores = [r.get("verifierScore") for r in rs if r.get("verifierScore") is not None]
        usages = [r.get("usage") or {} for r in rs]
        ins = [u.get("promptTokens") for u in usages if u.get("promptTokens")]
        outs = [u.get("completionTokens") for u in usages if u.get("completionTokens")]
        costs_usd = [r.get("cost_usd") for r in rs if r.get("cost_usd") is not None]
        rt["latencyP50Ms"] = _merge_avg(rt.get("latencyP50Ms"), prev_n, lats, 0)
        rt["avgVerifyScore"] = _merge_avg(rt.get("avgVerifyScore"), prev_n, scores)
        ok = [r for r in rs if (r.get("verifierScore") or 0) >= 7.0]
        prev_ok = float(rt.get("successRate") or 0) * prev_n
        rt["successRate"] = round((prev_ok + len(ok)) / (prev_n + len(rs)), 2)
        rt["samples"] = prev_n + len(rs)
        # v15.5 问题8：档案健康字段——lastSeenOK（最近成功时间）、verifyScoreStd（verifier 分采样方差）、
        # consecutiveFail（从熔断器 circuit-state 读连续失败计数）
        rt["lastSeenOK"] = now_shanghai().isoformat()
        if len(scores) >= 2:
            m = sum(scores) / len(scores)
            rt["verifyScoreStd"] = round(
                (sum((s - m) ** 2 for s in scores) / (len(scores) - 1)) ** 0.5, 2)
        try:
            cs = json.loads((BASE / "circuit-state.json").read_text(encoding="utf-8")) \
                if (BASE / "circuit-state.json").exists() else {}
            st = cs.get(cid) or {}
            cf = st.get("consecutiveFailures") or st.get("failures")
            if isinstance(cf, (int, float)):
                rt["consecutiveFail"] = int(cf)
        except Exception:
            pass
        ct = model.setdefault("cost", {})
        ct["avgInputTokens"] = _merge_avg(ct.get("avgInputTokens"), prev_n, ins, 0)
        ct["avgOutputTokens"] = _merge_avg(ct.get("avgOutputTokens"), prev_n, outs, 0)
        costs_cny = [c * fx for c in costs_usd]
        ct["costPerCallCny"] = _merge_avg(ct.get("costPerCallCny"), prev_n, costs_cny, 6)
    return new_caps


def _drift_paused():
    """P1-2：judge 漂移 |drift| ≥ pauseWriteDrift → 暂停写档案。返回 (paused, info)。"""
    try:
        jd = json.loads((BASE / "judge-drift.json").read_text(encoding="utf-8"))
    except Exception:
        return False, {}
    drift = jd.get("drift")
    if drift is None:
        return False, {}
    th = float(_fb_params().get("pauseWriteDrift", 0.5))
    if abs(float(drift)) >= th:
        return True, {"drift": drift, "pauseWriteDrift": th}
    return False, {}


def update_runtime_telemetry(dry: bool = False) -> dict:
    """P0-4：客观遥测自动回填（每 run 收尾调用；不 bump revision、不改能力分）。
    锁内：读 → 算 → 校验 → 原子写。无有效行 → skipped。"""
    rows = _load_feedback_rows()
    if not any(r.get("success") and r.get("model") for r in rows):
        return {"skipped": True, "reason": "no_telemetry_rows", "changedScores": 0}
    with file_lock(CAPS, timeout_s=LOCK_TIMEOUT_S, stale_after_s=LOCK_STALE_S):
        caps = json.loads(CAPS.read_text(encoding="utf-8"))
        old_revision = int(caps.get("revision") or 0)
        new_caps = plan_runtime_telemetry(caps, rows)
        new_caps["revision"] = old_revision  # 遥测不 bump revision（分数语义不变）
        validate_or_raise(new_caps, old_revision=old_revision, source="capabilities.json(runtime-telemetry)")
        if not dry:
            tmp = CAPS.with_suffix(".json.tmp")
            tmp.write_text(json.dumps(new_caps, ensure_ascii=False, indent=2), encoding="utf-8")
            tmp.replace(CAPS)
    return {"skipped": False, "revision": old_revision, "changedScores": 0,
            "telemetryUpdated": True}


def plan_update(caps: dict, runtime: dict, contributing_run_ids: set,
                total_runs: int) -> tuple:
    """计算融合计划（不落盘）。返回 (new_caps, summary)。"""
    fb = _fb_params()
    sample_knee = int(fb.get("sampleKnee", SAMPLE_KNEE))
    min_flip_runs = int(fb.get("minFlipRuns", MIN_FLIP_RUNS))
    min_change = float(fb.get("minScoreChange", 0.5))
    merge_weight = float(fb.get("mergeWeight", 0.7))
    z_scale = float(fb.get("zToScoreScale", 2.5))

    new_caps = json.loads(json.dumps(caps))  # 深拷贝
    if not contributing_run_ids:
        return new_caps, {"revision": int(new_caps.get("revision") or 0),
                          "changedScores": 0, "totalRuns": 0, "sourceRunIds": []}
    revision = int(new_caps.get("revision") or 0) + 1
    changed = 0
    for cid, model in new_caps.get("models", {}).items():
        rt = runtime.get(cid, {})
        for d, cap_entry in model.get("capabilities", {}).items():
            if not isinstance(cap_entry, dict):
                continue
            bench = cap_entry.get("score")
            if bench is None or d not in rt:
                continue
            n = rt[d]["n"]
            z_avg = rt[d]["zSum"] / n
            # z → 分制偏移（与 calibration 一致）
            runtime_score = bench + z_avg * z_scale
            runtime_score = max(0.0, min(10.0, runtime_score))
            w = min(n / sample_knee, 1.0)
            merged = round(bench * (1 - w * merge_weight) + runtime_score * w * merge_weight, 2)
            cap_entry["runtimeSamples"] = n
            # 最小改动阈值（设计 §3.5）：|Δ|<min_change 不写分，只更新置信度（防噪声抖动）；
            # 大改动需 ≥minFlipRuns 个有效 run 才允许（翻排名门槛，防小样本带偏）。
            delta = abs(merged - bench)
            if delta >= min_change and total_runs >= min_flip_runs:
                cap_entry["score"] = merged
                changed += 1
    new_caps["revision"] = revision
    new_caps["updatedAt"] = now_shanghai().isoformat()
    new_caps["runtimeFeedback"] = {
        "totalRuns": total_runs,
        "lastUpdateAt": now_shanghai().isoformat(),
        "sourceRunIds": sorted(contributing_run_ids),
        "minFlipRuns": min_flip_runs,
        "minScoreChange": min_change,
    }
    return new_caps, {"revision": revision, "changedScores": changed,
                      "totalRuns": total_runs,
                      "sourceRunIds": sorted(contributing_run_ids)}


def update(dry: bool = False) -> dict:
    """锁内：读 → 算 → 校验 → 原子写。无有效反馈 → skipped（不空转 revision）。
    P1-2：judge 漂移 |drift| ≥ pauseWriteDrift → 拒绝写档案。"""
    paused, drift_info = _drift_paused()
    if paused:
        return {"skipped": True, "reason": "judge_drift_paused",
                "revision": None, "changedScores": 0, "totalRuns": 0,
                "sourceRunIds": [], **drift_info}
    runtime, contributing, rejected_self = _feedback_to_runtime_scores()
    total_runs = len(contributing)
    if not runtime or total_runs == 0:
        return {"skipped": True, "reason": "no_valid_runtime_feedback",
                "revision": None, "changedScores": 0, "totalRuns": 0,
                "sourceRunIds": [], "rejectedSelfScored": rejected_self}
    with file_lock(CAPS, timeout_s=LOCK_TIMEOUT_S, stale_after_s=LOCK_STALE_S):
        caps = json.loads(CAPS.read_text(encoding="utf-8"))
        old_revision = int(caps.get("revision") or 0)
        new_caps, summary = plan_update(caps, runtime, contributing, total_runs)
        new_caps = plan_runtime_telemetry(new_caps, _load_feedback_rows())
        new_caps["revision"] = summary["revision"]
        validate_or_raise(new_caps, old_revision=old_revision, source="capabilities.json(runtime-update)")
        if not dry:
            backup = CAPS.with_name(f"capabilities.rev{old_revision}.json.bak")
            if not backup.exists():
                backup.write_text(json.dumps(caps, ensure_ascii=False, indent=2),
                                  encoding="utf-8")
            tmp = CAPS.with_suffix(".json.tmp")
            tmp.write_text(json.dumps(new_caps, ensure_ascii=False, indent=2), encoding="utf-8")
            tmp.replace(CAPS)
    summary["skipped"] = False
    summary["rejectedSelfScored"] = rejected_self
    return summary


def pending_diff() -> dict:
    """P0-5：只生成 pending diff（不落盘能力分）。council 收尾默认路径。
    人工审批后 `--apply` 才写档案。"""
    paused, drift_info = _drift_paused()
    if paused:
        return {"skipped": True, "reason": "judge_drift_paused",
                "pendingDiff": False, "revision": None, "changedScores": 0,
                "totalRuns": 0, "sourceRunIds": [], **drift_info}
    runtime, contributing, rejected_self = _feedback_to_runtime_scores()
    total_runs = len(contributing)
    if not runtime or total_runs == 0:
        return {"skipped": True, "reason": "no_valid_runtime_feedback",
                "pendingDiff": False, "revision": None, "changedScores": 0,
                "totalRuns": 0, "sourceRunIds": [], "rejectedSelfScored": rejected_self}
    caps = json.loads(CAPS.read_text(encoding="utf-8"))
    base_hash = _caps_hash(CAPS.read_bytes())
    new_caps, summary = plan_update(caps, runtime, contributing, total_runs)
    new_caps = plan_runtime_telemetry(new_caps, _load_feedback_rows())
    new_caps["revision"] = summary["revision"]
    diff = {"createdAt": now_shanghai().isoformat(),
            "baseHash": base_hash,
            "baseRevision": int(caps.get("revision") or 0),
            "summary": {**summary, "rejectedSelfScored": rejected_self},
            "newCapabilities": new_caps}
    PENDING.parent.mkdir(parents=True, exist_ok=True)
    tmp = PENDING.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(diff, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(PENDING)
    out = {**summary, "pendingDiff": True, "path": str(PENDING),
           "baseHash": base_hash[:12], "rejectedSelfScored": rejected_self}
    return out


def apply_pending() -> dict:
    """P0-5：人工审批后落盘。校验 baseHash（档案在 pending 生成后未被改动）+
    revision 单调 + caps_guard 结构校验，任一不满足拒绝。"""
    paused, drift_info = _drift_paused()
    if paused:
        return {"applied": False, "reason": "judge_drift_paused", **drift_info}
    if not PENDING.exists():
        return {"applied": False, "reason": "no_pending_diff"}
    pend = json.loads(PENDING.read_text(encoding="utf-8"))
    new_caps = pend.get("newCapabilities")
    if not isinstance(new_caps, dict):
        return {"applied": False, "reason": "pending_diff_malformed"}
    with file_lock(CAPS, timeout_s=LOCK_TIMEOUT_S, stale_after_s=LOCK_STALE_S):
        cur_bytes = CAPS.read_bytes()
        cur_hash = _caps_hash(cur_bytes)
        if cur_hash != pend.get("baseHash"):
            return {"applied": False, "reason": "baseHash_mismatch",
                    "expected": str(pend.get("baseHash"))[:12], "actual": cur_hash[:12],
                    "hint": "capabilities.json 在 pending 生成后被改动，人工审后再生成新 pending"}
        caps = json.loads(cur_bytes.decode("utf-8"))
        old_revision = int(caps.get("revision") or 0)
        validate_or_raise(new_caps, old_revision=old_revision,
                          source="capabilities.json(pending-apply)")
        backup = CAPS.with_name(f"capabilities.rev{old_revision}.json.bak")
        if not backup.exists():
            backup.write_text(json.dumps(caps, ensure_ascii=False, indent=2), encoding="utf-8")
        tmp = CAPS.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(new_caps, ensure_ascii=False, indent=2), encoding="utf-8")
        tmp.replace(CAPS)
        applied_ts = now_shanghai().strftime("%Y%m%d-%H%M%S")
        PENDING.replace(PENDING.with_name(f"pending-runtime-diff.applied-{applied_ts}.json"))
    return {"applied": True, "revision": pend["summary"].get("revision"),
            "changedScores": pend["summary"].get("changedScores"),
            "sourceRunIds": pend["summary"].get("sourceRunIds")}


def _count_runs():
    if not FEEDBACK.exists():
        return 0
    runs = set()
    for line in FEEDBACK.read_text(encoding="utf-8").splitlines():
        try:
            runs.add(json.loads(line).get("run_id"))
        except json.JSONDecodeError:
            pass
    return len(runs)


if __name__ == "__main__":
    if "--apply" in sys.argv:
        print(json.dumps(apply_pending(), ensure_ascii=False, indent=2))
    elif "--pending" in sys.argv:
        print(json.dumps(pending_diff(), ensure_ascii=False, indent=2))
    elif "--dry" in sys.argv:
        runtime, contributing, rejected_self = _feedback_to_runtime_scores()
        caps = json.loads(CAPS.read_text(encoding="utf-8"))
        new_caps, summary = plan_update(caps, runtime, contributing, len(contributing))
        print(json.dumps({**summary, "dry": True, "rejectedSelfScored": rejected_self},
                         ensure_ascii=False, indent=2))
    else:
        print(json.dumps(update(), ensure_ascii=False, indent=2))
