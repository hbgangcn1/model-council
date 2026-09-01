"""H2：Benchmark → 能力档案 ingester（断点续跑数据闭环 + 人工审批流）。

v15.1（评审报告 L 项：benchmark 结果直接写入档案风险较高 → diff 人工审批）：
- 默认 CLI 只生成 pending diff 文件（每处变化 old/new/caseIds 全列），不碰 capabilities.json；
- `--apply <diff>` 校验 baseHash（当前档案必须仍是 diff 的基线）后合入，revision 自增；
- `--direct` 保留直接摄入（低风险自动化场景）；文件锁 + 写前校验全覆盖；
- 幂等：已摄入过的 case id 重复出现不产生任何变化；
- 可回溯：每个维度记 _source_run_ids（case id 列表）。

用法：
  python benchmark/capability_ingest.py                 # 生成 pending diff（默认，人工审批流）
  python benchmark/capability_ingest.py --apply         # 审批通过后合入 pending diff
  python benchmark/capability_ingest.py --apply <file>  # 合入指定 diff
  python benchmark/capability_ingest.py --direct        # 直接摄入（跳过审批）
  python benchmark/capability_ingest.py --dry           # 只算计划不落盘
"""
import hashlib
import json
import sys
from pathlib import Path

try:  # 包内导入；直接执行时退回顶层导入
    from orchestrator.config_loader import now_shanghai
    from orchestrator.file_lock import file_lock
    from orchestrator.caps_guard import validate_or_raise
    from orchestrator import params as params_mod
except ImportError:
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
    from orchestrator.config_loader import now_shanghai
    from orchestrator.file_lock import file_lock
    from orchestrator.caps_guard import validate_or_raise
    from orchestrator import params as params_mod  # noqa: E402

BASE = Path(__file__).resolve().parent.parent  # council/
SCORES_ROOT = BASE / "benchmark" / "scores"
CAPS = BASE / "capabilities.json"
PENDING_DIFF = BASE / "benchmark" / "pending-ingest-diff.json"
ALPHA = 0.3          # EMA 权重默认值（params.ingest.emaAlpha 可覆盖）
FALLBACK_DIM_SCORE = 5.0  # 极端兜底（无历史分数且均值异常时）
LOCK_TIMEOUT_S = 30
LOCK_STALE_S = 120


def _ingest_params() -> dict:
    return params_mod.load().get("ingest", params_mod.DEFAULTS["ingest"])


def _file_sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _cases_hash() -> str | None:
    """P0-6：v21-cases.json 的 contentHash（无字段则回退文件 sha256），
    摄入时记入 ingestMeta，下次用例漂移可检测（分数不可比告警）。"""
    cases_file = BASE / "benchmark" / "v21-cases.json"
    if not cases_file.exists():
        return None
    try:
        doc = json.loads(cases_file.read_text(encoding="utf-8"))
        if isinstance(doc.get("contentHash"), str) and doc["contentHash"]:
            return doc["contentHash"]
    except (json.JSONDecodeError, OSError):
        pass
    return _file_sha256(cases_file)


def collect_cases(scores_root: Path = SCORES_ROOT) -> dict:
    """→ {cid: {dim: [(case_id, score, ts)]}}。跳过损坏/非 dict 行。"""
    out = {}
    if not scores_root.exists():
        return out
    for cid_dir in sorted(p for p in scores_root.iterdir() if p.is_dir()):
        dims = {}
        for f in sorted(cid_dir.glob("*.json")):
            try:
                rec = json.loads(f.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, OSError):
                continue
            if not isinstance(rec, dict) or rec.get("cand_id") != cid_dir.name:
                continue
            dim = rec.get("dimension")
            score = rec.get("score")
            if dim and isinstance(score, (int, float)):
                dims.setdefault(dim, []).append(
                    (f.stem, float(score), rec.get("ts", 0)))
        if dims:
            out[cid_dir.name] = dims
    return out


def plan_ingest(caps: dict, cases: dict, alpha: float = ALPHA) -> tuple:
    """计算合并计划（不落盘）。返回 (new_caps, summary)。"""
    new_caps = json.loads(json.dumps(caps))  # 深拷贝，不动原对象
    ingested_total = 0
    changed = 0
    per_cid = {}
    for cid, dims in cases.items():
        model = new_caps.get("models", {}).get(cid)
        if not model:
            continue  # 档案里没有的候选不摄入（避免凭空造档案条目）
        caps_dim = model.setdefault("capabilities", {})
        cid_changed = 0
        cid_ingested = 0
        for dim, rows in dims.items():
            entry = caps_dim.setdefault(dim, {"score": None, "samples": 0,
                                              "freshness": 1.0, "interpolated": False,
                                              "_source_run_ids": []})
            source_ids = set(entry.get("_source_run_ids") or [])
            new_rows = [(cid_, s, t) for cid_, s, t in rows if cid_ not in source_ids]
            if not new_rows:
                continue  # 幂等：已摄入过的 case 跳过
            mean = sum(s for _, s, _ in new_rows) / len(new_rows)
            mean = max(0.0, min(10.0, mean))
            bench = entry.get("score")
            if bench is None:
                new_score = mean
            else:
                new_score = max(0.0, min(10.0, round(bench * (1 - alpha) + mean * alpha, 2)))
            if new_score != entry.get("score"):
                changed += 1
                cid_changed += 1
            entry["score"] = new_score
            entry["samples"] = int(entry.get("samples") or 0) + len(new_rows)
            entry["freshness"] = 1.0
            entry["interpolated"] = False
            entry["_source_run_ids"] = sorted(source_ids | {c for c, _, _ in new_rows})
            ingested_total += len(new_rows)
            cid_ingested += len(new_rows)
        if cid_ingested:
            per_cid[cid] = {"ingestedCases": cid_ingested, "changedDims": cid_changed}
    summary = {"ingestedCases": ingested_total, "changedDims": changed,
               "perCandidate": per_cid}
    if ingested_total:
        new_caps["revision"] = int(new_caps.get("revision") or 0) + 1
        new_caps["updatedAt"] = now_shanghai().isoformat()
        new_caps["ingestMeta"] = {
            "source": "benchmark/scores",
            "lastIngestAt": now_shanghai().isoformat(),
            "ingestedCaseIdsTotal": int(
                (new_caps.get("ingestMeta") or {}).get("ingestedCaseIdsTotal") or 0) + ingested_total,
            "emaAlpha": alpha,
        }
    return new_caps, summary


def _diff_changes(caps: dict, new_caps: dict) -> list:
    """新旧档案逐维度对比 → 审批 diff 清单。"""
    changes = []
    for cid, model in new_caps.get("models", {}).items():
        old_model = caps.get("models", {}).get(cid) or {}
        for dim, entry in model.get("capabilities", {}).items():
            if not isinstance(entry, dict):
                continue
            old_entry = (old_model.get("capabilities") or {}).get(dim) or {}
            old_score = old_entry.get("score")
            new_score = entry.get("score")
            old_ids = set(old_entry.get("_source_run_ids") or [])
            new_ids = set(entry.get("_source_run_ids") or [])
            added = sorted(new_ids - old_ids)
            if new_score != old_score or added:
                changes.append({
                    "cid": cid, "dim": dim,
                    "oldScore": old_score, "newScore": new_score,
                    "oldSamples": old_entry.get("samples", 0),
                    "newSamples": entry.get("samples", 0),
                    "addedCaseIds": added,
                })
    return changes


def build_diff(caps_path: Path = CAPS, scores_root: Path = SCORES_ROOT,
               out_path: Path = PENDING_DIFF, alpha: float = None) -> dict:
    """计算计划 → 生成 pending diff 文件（不修改 capabilities.json）。"""
    alpha = alpha if alpha is not None else float(_ingest_params().get("emaAlpha", ALPHA))
    caps = json.loads(caps_path.read_text(encoding="utf-8"))
    cases = collect_cases(scores_root)
    new_caps, summary = plan_ingest(caps, cases, alpha)
    ch = _cases_hash()
    diff = {
        "generatedAt": now_shanghai().isoformat(),
        "baseHash": _file_sha256(caps_path),
        "baseRevision": int(caps.get("revision") or 0),
        "emaAlpha": alpha,
        "newCaseCount": summary["ingestedCases"],
        "changedDims": summary["changedDims"],
        "perCandidate": summary["perCandidate"],
        "casesHash": ch,
        "changes": _diff_changes(caps, new_caps),
        "scoresRootEmpty": not cases,  # P0-6：空目录显式告警，不再静默 skipped
        "note": "人工审批后执行 capability_ingest.py --apply 合入（或 --direct 跳过审批）。",
    }
    if not cases:
        diff["note"] = ("⚠ 警告：benchmark/scores 目录为空或不存在——本次 diff 无新数据。"
                        "若 scores/ 被移动/归档，请先修复路径或跑 bench/runner.py。") + diff["note"]
    tmp = out_path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(diff, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(out_path)
    return diff


def apply_diff(caps_path: Path = CAPS, diff_path: Path = PENDING_DIFF) -> dict:
    """审批通过后合入 diff：校验 baseHash → 逐项应用 changes → 校验 → 锁内原子写。"""
    diff = json.loads(diff_path.read_text(encoding="utf-8"))
    changes = diff.get("changes") or []
    if not changes:
        return {"skipped": True, "reason": "diff_has_no_changes"}
    with file_lock(caps_path, timeout_s=LOCK_TIMEOUT_S, stale_after_s=LOCK_STALE_S):
        caps = json.loads(caps_path.read_text(encoding="utf-8"))
        if _file_sha256(caps_path) != diff.get("baseHash"):
            raise ValueError(
                f"diff baseHash 与当前 {caps_path.name} 不匹配（档案已被其他更新修改）——"
                "请重新生成 diff 再审批")
        old_revision = int(caps.get("revision") or 0)
        expected_rev = int(diff.get("baseRevision") or 0)
        if old_revision != expected_rev:
            raise ValueError(f"diff 基于 revision {expected_rev}，当前为 {old_revision}——重新生成 diff")
        # P0-6：用例集漂移检查——diff 生成后 v21-cases.json 被改，分数不可比，拒绝合入
        if diff.get("casesHash") and _cases_hash() != diff.get("casesHash"):
            raise ValueError(
                "v21-cases.json 在 diff 生成后被修改（casesHash 不匹配）——"
                "用例漂移导致分数不可比，重新跑 benchmark 后再生成 diff")
        for ch in changes:
            model = caps.get("models", {}).get(ch["cid"])
            if not model:
                raise ValueError(f"diff 引用不存在的候选 {ch['cid']}——重新生成 diff")
            entry = model.setdefault("capabilities", {}).setdefault(
                ch["dim"], {"score": None, "samples": 0, "freshness": 1.0,
                            "interpolated": False, "_source_run_ids": []})
            if entry.get("score") != ch.get("oldScore"):
                raise ValueError(
                    f"{ch['cid']}.{ch['dim']} 当前分 {entry.get('score')!r} ≠ diff 基线 "
                    f"{ch.get('oldScore')!r}——档案已变，重新生成 diff")
            entry["score"] = ch["newScore"]
            entry["samples"] = int(ch.get("newSamples") or 0)
            entry["freshness"] = 1.0
            entry["interpolated"] = False
            entry["_source_run_ids"] = sorted(
                set(entry.get("_source_run_ids") or []) | set(ch.get("addedCaseIds") or []))
        caps["revision"] = old_revision + 1
        caps["updatedAt"] = now_shanghai().isoformat()
        caps["ingestMeta"] = {
            "source": "benchmark/scores",
            "lastIngestAt": now_shanghai().isoformat(),
            "approvedDiff": diff_path.name,
            "diffBaseHash": diff.get("baseHash"),
            "casesHash": diff.get("casesHash"),
            "ingestedCaseIdsTotal": int(
                (caps.get("ingestMeta") or {}).get("ingestedCaseIdsTotal") or 0)
                + int(diff.get("newCaseCount") or 0),
            "emaAlpha": diff.get("emaAlpha"),
        }
        validate_or_raise(caps, old_revision=old_revision, source="capabilities.json(ingest-apply)")
        backup = caps_path.with_name(f"capabilities.rev{old_revision}.json.bak")
        if not backup.exists():
            backup.write_text(json.dumps(caps, ensure_ascii=False, indent=2), encoding="utf-8")
        tmp = caps_path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(caps, ensure_ascii=False, indent=2), encoding="utf-8")
        tmp.replace(caps_path)
    # 合入成功：归档已审批的 diff（防止重复 apply）——
    # v15.5c（元评审 2026-08-26_21-50-46）：归档后删除原文件。此前只写归档副本不删原文件，
    # pending-ingest-diff.json 永久残留 → council_status 持续误报「590 个新 case 待审批」，
    # 元评审把已摄入的 590 case 当成「积压」异常信号（P1-1 误报根源）。
    applied_dir = diff_path.parent / "applied"
    applied_dir.mkdir(exist_ok=True)
    applied = applied_dir / f"{now_shanghai().strftime('%Y-%m-%d_%H-%M-%S')}-{diff_path.name}"
    try:
        applied.write_text(json.dumps(diff, ensure_ascii=False, indent=2), encoding="utf-8")
    except OSError:
        pass
    try:
        diff_path.unlink()
    except OSError:
        pass
    return {"skipped": False, "revision": old_revision + 1,
            "changedDims": len(changes), "newCaseCount": diff.get("newCaseCount")}


def ingest(caps_path: Path = CAPS, scores_root: Path = SCORES_ROOT,
           alpha: float = None, dry: bool = False) -> dict:
    """直接摄入（跳过审批；用于低风险自动化/测试）。锁 + 校验 + 原子写。"""
    alpha = alpha if alpha is not None else float(_ingest_params().get("emaAlpha", ALPHA))
    with file_lock(caps_path, timeout_s=LOCK_TIMEOUT_S, stale_after_s=LOCK_STALE_S):
        caps = json.loads(caps_path.read_text(encoding="utf-8"))
        cases = collect_cases(scores_root)
        new_caps, summary = plan_ingest(caps, cases, alpha)
        if not summary["ingestedCases"]:
            return {"skipped": True, "reason": "no_new_cases", **summary}
        old_revision = int(caps.get("revision") or 0)
        validate_or_raise(new_caps, old_revision=old_revision, source="capabilities.json(ingest-direct)")
        if not dry:
            backup = caps_path.with_name(f"capabilities.rev{old_revision}.json.bak")
            if not backup.exists():
                backup.write_text(json.dumps(caps, ensure_ascii=False, indent=2),
                                  encoding="utf-8")
            tmp = caps_path.with_suffix(".json.tmp")
            tmp.write_text(json.dumps(new_caps, ensure_ascii=False, indent=2), encoding="utf-8")
            tmp.replace(caps_path)
    return {"skipped": False, "dry": dry, "revision": new_caps.get("revision"), **summary}


if __name__ == "__main__":
    args = set(sys.argv[1:])
    if "--dry" in args:
        caps = json.loads(CAPS.read_text(encoding="utf-8"))
        cases = collect_cases(SCORES_ROOT)
        new_caps, summary = plan_ingest(
            caps, cases, float(_ingest_params().get("emaAlpha", ALPHA)))
        print(json.dumps({"dry": True, **summary}, ensure_ascii=False, indent=2))
    elif "--apply" in args:
        i = sys.argv.index("--apply")
        diff_path = Path(sys.argv[i + 1]) if i + 1 < len(sys.argv) else PENDING_DIFF
        print(json.dumps(apply_diff(CAPS, diff_path), ensure_ascii=False, indent=2))
    elif "--direct" in args:
        print(json.dumps(ingest(CAPS, SCORES_ROOT), ensure_ascii=False, indent=2))
    else:
        diff = build_diff(CAPS, SCORES_ROOT, PENDING_DIFF)
        print(json.dumps({
            "pendingDiff": str(PENDING_DIFF),
            "newCaseCount": diff["newCaseCount"],
            "changedDims": diff["changedDims"],
            "baseRevision": diff["baseRevision"],
            "note": diff["note"],
        }, ensure_ascii=False, indent=2))
