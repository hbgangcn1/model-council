"""v15.3：候选身份锚定（anchor_candidate.py）。

背景（2026-08-24 元评审实证）：stealth/ox-alpha 在 capabilities.json 里被
build_capabilities.py 按 "stealth" 前缀写死 identityUnknown=true/stable=false，
导致护栏每次选择都剔除它（24h 306 条 identity_unknown 噪声）。但 maintainer has在
settings.yaml 正式配置 openrouter-stealth provider（OpenRouter 路由 + API key），
模型应正式可用。锚定 = 把档案标记与真实配置对齐（identityUnknown→false, stable→true）。

安全契约：
1. 只允许锚定「identityUnknown=true 或 stable=false」的条目（防误改健康条目）；
2. 锚定前备份 capabilities.json（capabilities.rev{N}.json.bak，与 update_capabilities 同构）；
3. revision 自增 + caps_guard 写前校验 + 原子写；
4. 若存在 evals/pending-runtime-diff.json，同步 baseHash/baseRevision +
   newCapabilities.models 里同批条目的标记——否则 apply_pending 因 baseHash_mismatch 拒绝；
5. 同步建议（不自动执行，打印提示）：benchmark/build_capabilities.py 应改为显式状态表
   （CANDIDATE_STATUS），防止 rebuild 时按前缀写死旧标记。

用法：
  python orchestrator/anchor_candidate.py --model stealth/ox-alpha --reason "maintainer has在 settings.yaml 配置 openrouter-stealth（路由+key），正式启用"
"""
import argparse
import json
import sys
from pathlib import Path

try:
    from .caps_guard import validate_or_raise
except ImportError:
    from caps_guard import validate_or_raise  # type: ignore

BASE = Path(__file__).resolve().parent.parent  # council/
CAPS = BASE / "capabilities.json"
PENDING = BASE / "evals" / "pending-runtime-diff.json"


def _caps_hash(raw: bytes) -> str:
    import hashlib
    return hashlib.sha256(raw).hexdigest()


def anchor(base_model: str, reason: str) -> dict:
    caps = json.loads(CAPS.read_text(encoding="utf-8"))
    old_revision = int(caps.get("revision") or 0)
    models = caps.get("models", {})
    targets = {cid: cand for cid, cand in models.items()
               if cand.get("baseModel") == base_model}
    if not targets:
        return {"anchored": 0, "reason": f"档案中无 baseModel={base_model} 的条目"}
    for cid, cand in targets.items():
        if not (cand.get("identityUnknown") or not cand.get("stable", True)):
            return {"anchored": 0,
                    "reason": f"拒绝：{cid} 已是健康条目（identityUnknown=false, stable=true），无需锚定"}
    backup = CAPS.with_name(f"capabilities.rev{old_revision}.json.bak")
    if not backup.exists():
        backup.write_text(json.dumps(caps, ensure_ascii=False, indent=2), encoding="utf-8")
    for cid, cand in targets.items():
        cand["identityUnknown"] = False
        cand["stable"] = True
    new_revision = old_revision + 1
    caps["revision"] = new_revision
    caps.setdefault("meta", {})["anchored"] = {
        "baseModel": base_model, "cids": sorted(targets), "atRevision": new_revision,
        "reason": reason}
    validate_or_raise(caps, old_revision=old_revision, source="capabilities.json(anchor)")
    tmp = CAPS.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(caps, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(CAPS)

    # 同步 pending diff（P0-5 审批流 baseHash 契约）
    pending_synced = False
    if PENDING.exists():
        pend = json.loads(PENDING.read_text(encoding="utf-8"))
        nc = pend.get("newCapabilities")
        if isinstance(nc, dict) and isinstance(nc.get("models"), dict):
            synced = 0
            for cid in sorted(targets):
                if cid in nc["models"]:
                    nc["models"][cid]["identityUnknown"] = False
                    nc["models"][cid]["stable"] = True
                    synced += 1
            nc["revision"] = max(int(nc.get("revision") or 0), new_revision)
            pend["baseHash"] = _caps_hash(CAPS.read_bytes())
            pend["baseRevision"] = new_revision
            ptmp = PENDING.with_suffix(".json.tmp")
            ptmp.write_text(json.dumps(pend, ensure_ascii=False, indent=2), encoding="utf-8")
            ptmp.replace(PENDING)
            pending_synced = synced > 0
    return {"anchored": len(targets), "cids": sorted(targets),
            "revision": old_revision, "newRevision": new_revision,
            "backup": str(backup), "pendingSynced": pending_synced,
            "hint": "同步修改 benchmark/build_capabilities.py：stable/identityUnknown 改为显式状态表（CANDIDATE_STATUS），防止 rebuild 复活旧标记"}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--model", required=True, help="要锚定的 baseModel（如 stealth/ox-alpha）")
    ap.add_argument("--reason", default="", help="锚定理由（写入档案 meta.anchored）")
    args = ap.parse_args()
    result = anchor(args.model, args.reason)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    sys.exit(0 if result.get("anchored") else 1)


if __name__ == "__main__":
    main()
