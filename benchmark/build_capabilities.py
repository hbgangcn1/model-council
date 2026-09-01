"""capabilities.json 生成器：全档位实测分数（v15.5 插值废除）+ 定价占位。

v15.5：档位枚举 = host-side tier-bridge全档位（全档位×全案例，the maintainer decided）；插值逻辑删除，
档案不再出现 interpolated 条目。vendorGroup 从桥文件读取（v15.5-C 厂商互斥用）。
"""
import json
import sys
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent  # council/
BENCH = BASE / "benchmark"
if str(BASE) not in sys.path:
    sys.path.insert(0, str(BASE))
import bridge  # noqa: E402
import pool  # noqa: E402  v15.5-K：池名单为成员准入第一层

# v15.3：候选状态显式表（取代此前按 "stealth" 前缀写死 stable=false/identityUnknown=true 的
# 逻辑——2026-08-24 元评审实证该写死与 maintainer at settings.yaml 的正式配置脱节，
# 造成 24h 306 条 identity_unknown 护栏噪声）。未列出的模型走保守默认（stable=true、
# identityUnknown=false）。2026-08-27：OpenRouter 退役，stealth 条目移除，表保留备用。
CANDIDATE_STATUS = {
}
DIMENSIONS = ["reasoning", "code", "chinese", "research",
              "instruction_following", "long_context",
              "tool_use", "creativity", "safety"]


def main():
    summary = json.loads((BENCH / "scores-summary.json").read_text(encoding="utf-8"))
    cands = summary.get("candidates", {})

    models = {}
    pool_models = pool.members()  # v15.5-K：只为池成员生成档案条目
    for model in pool_models:
        entry = bridge.model_entry(model)
        provider = entry.get("provider", "unknown")
        vendor = entry.get("vendorGroup", "")
        for level in bridge.levels_for(model):
            cid = model.replace("/", "--") + "__" + level
            if cid not in cands:
                print(f"⚠️ 缺 {cid} 基准数据，跳过该档位（全档位×全案例要求下应补跑）",
                      file=sys.stderr)
                continue
            dims = cands[cid].get("dims", {})
            caps = {}
            for d in DIMENSIONS:
                v = dims.get(d)
                if v is not None:
                    caps[d] = {"score": v,
                               "samples": cands[cid].get("n_valid", 0),
                               "freshness": 1.0}
            models[cid] = {
                "baseModel": model,
                "thinking": level,
                "provider": provider,
                "vendorGroup": vendor,   # v15.5-C：厂商分组（verifier 厂商互斥用）
                "tier": ("T0-free" if model.startswith("MiniMax")
                         else "T1-pay-per-token"),
                "stable": CANDIDATE_STATUS.get(model, {}).get("stable", True),
                "identityUnknown": CANDIDATE_STATUS.get(model, {}).get("identityUnknown", False),
                "capabilities": caps,
                "runtime": {"avgVerifyScore": None, "successRate": None, "samples": 0},
                "cost": {"avgInputTokens": None, "avgOutputTokens": None, "costPerCallCny": None},
            }

    out = {
        "schemaVersion": 2,
        "generatedAt": None,
        "dimensions": DIMENSIONS,
        "models": models,
        "meta": {"source": "benchmark-v2.1-all-tiers (v15.5 全档位×全案例)",
                 "note": "成本字段待 pricing 档案与 usage 实测回填"},
    }
    import datetime
    out["generatedAt"] = datetime.datetime.now().astimezone().isoformat()
    target = BASE / "capabilities.json"
    tmp = target.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(out, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(target)
    print(f"✅ 生成 {target}（{len(models)} 个候选条目，全实测）")


if __name__ == "__main__":
    main()
