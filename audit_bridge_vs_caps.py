"""桥-档案-池-价四方对账审计（2026-09-06 评审建议 #4 落地，2026-09-06 扩为四方）。

capabilities.json（能力档）/ model-tier-bridge.json（跑分机档位桥）/
model-pool.json（池成员名单）/ pricing-profiles.json（定价口径）是四套
独立维护的同一现实映射，历史上已分叉过两次（孤儿模型、vendorGroup 不一致；
2026-09-06 又发现池漏 Muse / 池留已退役 glm / 定价缺 opencode-zen 三处分叉）。
本脚本对账四方，发现 drift 即报详情；--fix 只输出待办提议（写
`evals/pending-pool-changes.json`），绝不直接改池名单。
2026-10-03 Robert 拍板：池的增删只能手动，自动检测只提议+告警。
增删模型=控制台手动操作 + 跑一遍本脚本 --check 确认一致。

提议策略（2026-10-03 修订，原 2026-09-06 自动收敛已废止）：
- caps∩bridge 有、池无 → 提议加回 active（等 Robert 控制台确认）。
- 池 active 但 bridge/caps 双无 → 提议退役（等 Robert 控制台确认）。
- 池 active、在 bridge 但 caps 无 → 待跑分，保持 active，只报告不动。
- 定价缺 provider → 只报告不动（付费单价需人工核价，--fix 不编造数字）。

用法：
    python audit_bridge_vs_caps.py --check        # 0=一致，非 0=分叉（打详情）
    python audit_bridge_vs_caps.py --check --json # 机器可读
    python audit_bridge_vs_caps.py --fix          # 只输出提议（pending-pool-changes.json），不改池
"""
import argparse
import json
import sys
from pathlib import Path

BASE = Path(__file__).resolve().parent
CAPS_FILE = BASE / "capabilities.json"
BRIDGE_FILE = BASE / "model-tier-bridge.json"
POOL_FILE = BASE / "model-pool.json"
PRICING_FILE = BASE / "pricing-profiles.json"


def base_of(cid: str) -> str:
    return cid.rsplit("__", 1)[0] if "__" in cid else cid


def audit():
    """返回 (problems: list[str], info: dict)。problems 为空=一致。"""
    problems = []
    info = {}
    try:
        caps = json.loads(CAPS_FILE.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as e:
        return ["capabilities.json 不可读：%s" % e], {}
    try:
        bridge = json.loads(BRIDGE_FILE.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as e:
        return ["model-tier-bridge.json 不可读：%s" % e], {}
    caps_models = caps.get("models") or {}
    bridge_models = bridge.get("models") or {}
    caps_bases = set()
    caps_vendor = {}
    for cid, m in caps_models.items():
        if not isinstance(m, dict):
            continue
        b = m.get("baseModel") or base_of(cid)
        caps_bases.add(b)
        if m.get("vendorGroup"):
            caps_vendor.setdefault(b, set()).add(m["vendorGroup"])
    bridge_bases = set(bridge_models.keys())
    bridge_vendor = {b: (m or {}).get("vendorGroup") for b, m in bridge_models.items()
                     if isinstance(m, dict)}
    for b in sorted(bridge_bases - caps_bases):
        problems.append("孤儿桥条目：%s 在 bridge 有、caps 无（跑分机能跑、selector 选不到）" % b)
    for b in sorted(caps_bases - bridge_bases):
        problems.append("桥缺失条目：%s 在 caps 有、bridge 无（跑分机解析档位失败）" % b)
    for b in sorted(caps_bases & bridge_bases):
        cv = caps_vendor.get(b) or set()
        bv = bridge_vendor.get(b)
        if bv and cv and bv not in cv:
            problems.append("vendorGroup 不一致：%s caps=%s bridge=%s" % (b, sorted(cv), bv))
    # ---- 池对账（2026-09-06 新增）：档在即成员 ----
    try:
        pool_doc = json.loads(POOL_FILE.read_text(encoding="utf-8"))
        pool_active = {m.get("model") for m in pool_doc.get("models", [])
                       if isinstance(m, dict) and m.get("status") == "active"}
    except (OSError, json.JSONDecodeError) as e:
        return problems + ["model-pool.json 不可读：%s" % e], info
    runnable = caps_bases & bridge_bases
    for b in sorted(runnable - pool_active):
        problems.append("池缺失条目：%s 在 caps+bridge 有、池无（selector 会选中但反馈环丢弃；--fix 只提议，需 Robert 手动加回）" % b)
    for b in sorted(pool_active - caps_bases - bridge_bases):
        problems.append("池孤儿条目：%s 在池 active 但 caps/bridge 双无（--fix 只提议，需 Robert 手动退役）" % b)
    for b in sorted((pool_active & bridge_bases) - caps_bases):
        problems.append("池待跑分条目：%s 在池+bridge 有、caps 无（保持 active，先跑分；--fix 不动）" % b)
    # ---- 价对账（2026-09-06 新增）：caps provider 必须有定价口径 ----
    try:
        pricing = json.loads(PRICING_FILE.read_text(encoding="utf-8"))
        priced = set((pricing.get("providers") or {}).keys())
    except (OSError, json.JSONDecodeError) as e:
        return problems + ["pricing-profiles.json 不可读：%s" % e], info
    caps_providers = set()
    for m in caps_models.values():
        if isinstance(m, dict) and m.get("provider"):
            caps_providers.add(m["provider"])
    for p in sorted(caps_providers - priced):
        problems.append("定价缺失 provider：%s 在 caps 有、定价无（成本函数无口径；--fix 不编造，需人工核价补）" % p)
    info = {"capsBases": len(caps_bases), "bridgeBases": len(bridge_bases),
            "poolActive": len(pool_active), "runnable": len(runnable)}
    return problems, info


def fix():
    """只算提议、不改池（2026-10-03 全手动政策）：把加回/退役候选写入
    `evals/pending-pool-changes.json`，由 Robert 在控制台手动确认后执行。
    返回 (proposals, problems)。定价缺口只报告不动。"""
    problems, _ = audit()
    proposals = []
    pool_doc = json.loads(POOL_FILE.read_text(encoding="utf-8"))
    models = pool_doc.get("models", [])
    caps = json.loads(CAPS_FILE.read_text(encoding="utf-8")).get("models") or {}
    bridge = json.loads(BRIDGE_FILE.read_text(encoding="utf-8")).get("models") or {}
    caps_bases = set()
    for m in caps.values():
        if isinstance(m, dict):
            b = m.get("baseModel")
            if b:
                caps_bases.add(b)
    bridge_bases = set(bridge.keys())
    runnable = caps_bases & bridge_bases
    active = {m.get("model") for m in models
              if isinstance(m, dict) and m.get("status") == "active"}
    for b in sorted(runnable - active):
        proposals.append({"action": "propose_add", "model": b,
                          "reason": "caps+bridge 有、池无；请 Robert 控制台确认加回"})
    for m in models:
        if (isinstance(m, dict) and m.get("status") == "active"
                and m.get("model") not in caps_bases
                and m.get("model") not in bridge_bases):
            proposals.append({"action": "propose_retire", "model": m.get("model"),
                              "reason": "池 active 但 caps/bridge 双无；请 Robert 控制台确认退役"})
    out = {"proposals": proposals,
           "note": "全手动政策（2026-10-03）：本文件只记录提议，池名单只能由 Robert 手动改"}
    pend = BASE / "evals" / "pending-pool-changes.json"
    tmp = pend.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(out, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(pend)
    return proposals, problems


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--check", action="store_true", help="对账，0=一致")
    ap.add_argument("--json", action="store_true", help="机器可读输出")
    ap.add_argument("--fix", action="store_true", help="只输出池变更提议（不改池）后重查")
    args = ap.parse_args()
    if args.fix:
        proposals, _ = fix()
        if proposals:
            print("池变更提议（%d 项，已写 evals/pending-pool-changes.json；请 Robert 控制台手动确认）：" % len(proposals))
            for p in proposals:
                print("  ? %s %s：%s" % (p["action"], p["model"], p["reason"]))
        else:
            print("池无需变更。")
    problems, info = audit()
    if args.json:
        print(json.dumps({"ok": not problems, "problems": problems, "info": info},
                         ensure_ascii=False, indent=2))
    else:
        if problems:
            print("桥-档案对账 FAIL（%d 项）：" % len(problems))
            for p in problems:
                print("  - " + p)
        else:
            print("桥-档案对账 OK：%s" % json.dumps(info, ensure_ascii=False))
    return 0 if not problems else 2


if __name__ == "__main__":
    sys.exit(main())
