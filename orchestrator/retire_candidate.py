"""v15.3：退役候选条目（retire_candidate.py）。

背景（2026-08-24 元评审实证）：stealth/ox-alpha 三档 identityUnknown=true + stable=false，
从未被任何 run 选中（24h 内 306 条 identity_unknown 护栏事件 100% 来自它，每次 select
重复预过滤并污染 feedback 信噪比）。治理方式=从能力档案移除并归档，而非留在池里反复剔除。

安全契约：
1. 只允许退役「identityUnknown=true 或 stable=false」的候选（防误退役健康条目）；
2. 退役前备份 capabilities.json（capabilities.rev{N}.json.bak，与 update_capabilities 同构）；
3. revision 自增 + caps_guard 写前校验 + 原子写；
4. 若存在 evals/pending-runtime-diff.json，同步：从 newCapabilities.models 移除同 baseModel 条目、
   更新 baseHash/baseRevision——否则 apply_pending 会因 baseHash_mismatch 永久拒绝；
5. 同步建议（不自动执行，打印提示）：benchmark/build_capabilities.py 的 TIERS/MEASURED
   应移除该模型，防止未来 rebuild 复活。

用法：
  python orchestrator/retire_candidate.py --model stealth/ox-alpha --reason "身份无法验证（OpenRouter 第三方），24h 护栏噪声 306 次，从未被选中"
  python orchestrator/retire_candidate.py --list   # 列出当前 identityUnknown/stable=false 的候选
"""
import argparse
import json
import sys
from pathlib import Path

try:
    from . import caps_guard
    from .caps_guard import validate_or_raise
except ImportError:
    import caps_guard  # type: ignore
    from caps_guard import validate_or_raise  # type: ignore

BASE = Path(__file__).resolve().parent.parent  # council/
CAPS = BASE / "capabilities.json"
PENDING = BASE / "evals" / "pending-runtime-diff.json"


def _caps_hash(raw: bytes) -> str:
    import hashlib
    return hashlib.sha256(raw).hexdigest()


def list_retirable() -> list:
    """→ [{cid, baseModel, identityUnknown, stable}]（identityUnknown 或 stable=false 的候选）。"""
    caps = json.loads(CAPS.read_text(encoding="utf-8"))
    out = []
    for cid, cand in caps.get("models", {}).items():
        if cand.get("identityUnknown") or not cand.get("stable", True):
            out.append({"cid": cid, "baseModel": cand.get("baseModel"),
                        "identityUnknown": bool(cand.get("identityUnknown")),
                        "stable": bool(cand.get("stable", True))})
    return out


def retire(base_model: str, reason: str) -> dict:
    caps = json.loads(CAPS.read_text(encoding="utf-8"))
    old_revision = int(caps.get("revision") or 0)
    models = caps.get("models", {})
    victims = {cid: cand for cid, cand in models.items()
               if cand.get("baseModel") == base_model}
    if not victims:
        return {"retired": 0, "reason": f"档案中无 baseModel={base_model} 的条目"}
    # 安全契约：只允许退役身份未知或不稳定条目
    for cid, cand in victims.items():
        if not (cand.get("identityUnknown") or not cand.get("stable", True)):
            return {"retired": 0,
                    "reason": f"拒绝：{cid} 是健康条目（identityUnknown=false, stable=true），退役需人工确认"}
    # 备份（与 update_capabilities 同构，只备份一次）
    backup = CAPS.with_name(f"capabilities.rev{old_revision}.json.bak")
    if not backup.exists():
        backup.write_text(json.dumps(caps, ensure_ascii=False, indent=2), encoding="utf-8")
    for cid in list(victims):
        del models[cid]
    new_revision = old_revision + 1
    caps["revision"] = new_revision
    caps.setdefault("meta", {})["retired"] = {
        "baseModel": base_model, "cids": sorted(victims), "atRevision": new_revision,
        "reason": reason}
    validate_or_raise(caps, old_revision=old_revision, source="capabilities.json(retire)")
    tmp = CAPS.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(caps, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(CAPS)

    # 同步 pending diff（P0-5 审批流的 baseHash 契约）
    pending_synced = False
    if PENDING.exists():
        pend = json.loads(PENDING.read_text(encoding="utf-8"))
        nc = pend.get("newCapabilities")
        if isinstance(nc, dict) and isinstance(nc.get("models"), dict):
            removed_pending = 0
            for cid in list(victims):
                if cid in nc["models"]:
                    del nc["models"][cid]
                    removed_pending += 1
            nc["revision"] = max(int(nc.get("revision") or 0), new_revision)
            pend["baseHash"] = _caps_hash(CAPS.read_bytes())
            pend["baseRevision"] = new_revision
            pend.setdefault("meta", {})["retiredAtPending"] = {
                "baseModel": base_model, "cids": sorted(victims), "reason": reason}
            ptmp = PENDING.with_suffix(".json.tmp")
            ptmp.write_text(json.dumps(pend, ensure_ascii=False, indent=2), encoding="utf-8")
            ptmp.replace(PENDING)
            pending_synced = removed_pending > 0
    return {"retired": len(victims), "cids": sorted(victims),
            "revision": old_revision, "newRevision": new_revision,
            "backup": str(backup), "pendingSynced": pending_synced,
            "hint": f"同步修改 benchmark/build_capabilities.py：TIERS/MEASURED 移除 {base_model}，防止 rebuild 复活"}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--model", help="要退役的 baseModel（如 stealth/ox-alpha）")
    ap.add_argument("--reason", default="", help="退役原因（写入档案 meta.retired）")
    ap.add_argument("--list", action="store_true", help="列出可退役候选")
    args = ap.parse_args()
    if args.list:
        rows = list_retirable()
        if not rows:
            print("无可退役候选（全部条目 identityUnknown=false 且 stable=true）")
            return
        for r in rows:
            print(f"  {r['cid']}  identityUnknown={r['identityUnknown']} stable={r['stable']}")
        return
    if not args.model:
        ap.error("--model 或 --list 二选一")
    result = retire(args.model, args.reason)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    sys.exit(0 if result.get("retired") else 1)


if __name__ == "__main__":
    main()
