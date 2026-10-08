"""judge 自动选择（零硬编码模型名）。

解析顺序（调用方约定）：显式 params 值 > judge_select() > last-known-good 文件。
本模块只负责中间两段：按数据排名 + 读 last-known-good 兜底；显式 override 由调用方
（如 judge_drift）先判断，本模块不读 params。

select_judge(pool_members, caps, banned_base_models, balance_snapshot)
  -> ranked list of (model, thinking)，best-first：
- 有 judge-profiles.json 评卷数据时按 judging 分排序（高者优先）；
  档案条目若缺 judgingScore 字段，则由 calibrationError/discrimination 现场推导，
  推导不出才视为无数据；
- 无评卷数据的候选按 capabilities 维度均值回退排序（不 crash）；
- hetero 规则：baseModel 落在 banned_base_models 里的候选直接剔除；
- 额度快照（可选）：被标记耗尽的候选沉底但不剔除（fail-open），快照缺失/损坏一律忽略；
- 全被 ban / 池空 → 返回 []（由调用方接 last-known-good 或 fail-loud），绝不捏造默认值。

本文件不包含任何字面模型名默认值：所有模型名只来自传入参数或磁盘数据文件。
"""
import json
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent  # council/
PROFILE_FILES = (BASE / "judge-profiles.json", BASE / "benchmark" / "judge-profiles.json")
CAPS_FILE = BASE / "capabilities.json"
DRIFT_OUT = BASE / "judge-drift.json"
BASELINE = BASE / "benchmark" / "golden" / "judge-baseline.json"

_FALLBACK_THINKING = "low"
_PREFERRED_THINKING = "low"


def _load_json(path: Path):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None


def _models_of(caps) -> dict:
    if isinstance(caps, dict) and isinstance(caps.get("models"), dict):
        return caps["models"]
    return caps if isinstance(caps, dict) else {}


def _base_of(key: str, entry: dict) -> str:
    base = (entry or {}).get("baseModel")
    if isinstance(base, str) and base.strip():
        return base.strip()
    return str(key).split("__")[0].strip()


def _thinking_of(key: str, entry: dict) -> str:
    lv = (entry or {}).get("thinking")
    if isinstance(lv, str) and lv.strip():
        return lv.strip()
    parts = str(key).split("__")
    return parts[1].strip() if len(parts) > 1 and parts[1].strip() else _FALLBACK_THINKING


def _eligible(entry: dict) -> bool:
    """仅显式不合格才剔除（字段缺失一律放行，fail-open）。"""
    if entry.get("stable") is False:
        return False
    if entry.get("identityUnknown") is True:
        return False
    return True


def judging_score(entry: dict):
    """档案条目 -> judging 分（越高越好），无数据返回 None。

    字段缺失时现场推导：discrimination（越高越好）减 calibrationError（越低越好），
    与直接记录的 judgingScore 同向（越高越好）。
    """
    if not isinstance(entry, dict):
        return None
    direct = entry.get("judgingScore")
    if isinstance(direct, (int, float)):
        return float(direct)
    calib = entry.get("calibrationError")
    disc = entry.get("discrimination")
    if isinstance(disc, (int, float)) and isinstance(calib, (int, float)):
        return float(disc) - float(calib)
    if isinstance(disc, (int, float)):
        return float(disc)
    if isinstance(calib, (int, float)):
        return -float(calib)
    return None


def caps_mean(entry: dict):
    """能力维度均值（回退排序用），无维度分返回 None。"""
    caps = (entry or {}).get("capabilities") or {}
    vals = [v.get("score") for v in caps.values()
            if isinstance(v, dict) and isinstance(v.get("score"), (int, float))]
    if not vals:
        return None
    return sum(vals) / len(vals)


def _exhausted(snapshot) -> set:
    if not isinstance(snapshot, dict):
        return set()
    out = set()
    for key in ("exhausted", "quotaExhausted", "banned"):
        val = snapshot.get(key)
        if isinstance(val, (list, tuple, set)):
            out.update(str(m).strip() for m in val if str(m).strip())
    return out


def _provider_exhausted(entry: dict, snapshot) -> bool:
    """provider 窗口剩余额度为 0（如快照含 "<provider>..." 键且值为 0）→ 沉底。"""
    if not isinstance(snapshot, dict):
        return False
    provider = str((entry or {}).get("provider") or "").strip()
    if not provider:
        return False
    for key, val in snapshot.items():
        if not isinstance(key, str) or not isinstance(val, (int, float)):
            continue
        if provider in key and float(val) <= 0:
            return True
    return False


def select_judge(pool_members, caps, banned_base_models=None, balance_snapshot=None):
    """按评卷分（无则能力均值）排名，返回 [(model, thinking)]，best-first。"""
    banned = {str(m).strip() for m in (banned_base_models or []) if str(m).strip()}
    exhausted = _exhausted(balance_snapshot)
    models_doc = _models_of(caps)
    profiles = _load_json(PROFILE_FILES[0]) or _load_json(PROFILE_FILES[1]) or {}
    judges = profiles.get("judges") if isinstance(profiles, dict) else None
    judges = judges if isinstance(judges, dict) else {}

    pool = [str(m).strip() for m in (pool_members or []) if str(m).strip()]
    by_model: dict = {}
    for key, entry in models_doc.items():
        if not isinstance(entry, dict):
            continue
        base = _base_of(key, entry)
        if base not in pool or base in banned or not _eligible(entry):
            continue
        slot = by_model.setdefault(base, [])
        slot.append((_thinking_of(key, entry), caps_mean(entry), entry))

    rows = []
    for base in pool:
        if base in banned:
            continue
        variants = by_model.get(base) or []
        prof = judges.get(base) if isinstance(judges.get(base), dict) else None
        score = judging_score(prof) if prof is not None else None
        if score is not None:
            level = None
            if isinstance(prof.get("level"), str) and prof["level"].strip():
                level = prof["level"].strip()
            elif variants:
                preferred = [t for t, _, _ in variants if t == _PREFERRED_THINKING]
                level = preferred[0] if preferred else sorted(t for t, _, _ in variants)[0]
            else:
                level = _FALLBACK_THINKING
            entry = variants[0][2] if variants else {}
            rows.append({"model": base, "thinking": level, "score": score,
                         "judged": True, "entry": entry})
            continue
        if not variants:
            continue
        best = max(variants, key=lambda v: (v[1] is not None, v[1] if v[1] is not None else 0.0))
        if best[1] is None:
            continue
        rows.append({"model": base, "thinking": best[0], "score": best[1],
                     "judged": False, "entry": best[2]})

    any_judged = any(r["judged"] for r in rows)
    if any_judged:
        judged = sorted((r for r in rows if r["judged"]),
                        key=lambda r: (-r["score"], r["model"], r["thinking"]))
        fallback = sorted((r for r in rows if not r["judged"]),
                          key=lambda r: (-r["score"], r["model"], r["thinking"]))
        rows = judged + fallback
    else:
        rows = sorted(rows, key=lambda r: (-r["score"], r["model"], r["thinking"]))

    def _sunk(r):
        return (r["model"] in exhausted
                or _provider_exhausted(r["entry"], balance_snapshot))

    live = [r for r in rows if not _sunk(r)]
    sunk = [r for r in rows if _sunk(r)]
    return [(r["model"], r["thinking"]) for r in live + sunk]


def last_known_good(pool_members=None, banned_base_models=None):
    """读 judge-drift.json / judge-baseline.json 的 judge 字段兜底。

    仅当候选仍在池内且不在 ban 名单才返回，否则返回 None。"""
    banned = {str(m).strip() for m in (banned_base_models or []) if str(m).strip()}
    pool = ({str(m).strip() for m in (pool_members or []) if str(m).strip()}
            if pool_members is not None else None)
    for path in (DRIFT_OUT, BASELINE):
        doc = _load_json(path)
        if not isinstance(doc, dict):
            continue
        raw = str(doc.get("judge") or "").strip()
        if "@" not in raw:
            continue
        model, thinking = raw.rsplit("@", 1)
        model, thinking = model.strip(), thinking.strip()
        if not model or not thinking:
            continue
        if model in banned:
            continue
        if pool is not None and model not in pool:
            continue
        return (model, thinking)
    return None


def resolve_auto(banned_base_models=None):
    """零参数便捷入口：从磁盘加载池/档案/快照并排名（快照失败一律放行）。"""
    try:
        import sys
        sys.path.insert(0, str(BASE))
        sys.path.insert(0, str(BASE / "orchestrator"))
        import pool as pool_mod
        members = pool_mod.members()
    except Exception:
        members = None
    caps = _load_json(CAPS_FILE)
    snapshot = None
    try:
        from orchestrator import query_balance as _qb
        snapshot = _qb.query()
    except Exception:
        try:
            import query_balance as _qb2
            snapshot = _qb2.query()
        except Exception:
            snapshot = None
    if members is None:
        models_doc = _models_of(caps)
        members = sorted({_base_of(k, e) for k, e in models_doc.items()
                          if isinstance(e, dict) and _eligible(e)})
    return select_judge(members, caps or {}, banned_base_models, snapshot)
