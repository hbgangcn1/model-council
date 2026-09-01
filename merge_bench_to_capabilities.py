#!/usr/bin/env python3
"""merge_bench_to_capabilities.py — 把 benchmark/scores/ 合并到 capabilities.json

一次性脚本：v15.6 L3 接入 glm-5.3-flash 后，把 benchmark 跑分结果（184/185 case verdict=real）合并到
capabilities.json 能力档案，让 selector 下次 council 任务能选到 glm-5.3-flash。

与 v15.2 自进化机制 update_capabilities.py --apply 的区别：
- update_capabilities.py 只处理 runs/ 目录（council 任务产物）
- 本脚本只处理 benchmark/scores/ 目录（benchmark 跑分产物）
- 两者互补不冲突：council 任务走 update_capabilities.py；benchmark 走本脚本

用法：
    python merge_bench_to_capabilities.py                # dry-run（只统计 + 打印，不写）
    python merge_bench_to_capabilities.py --apply      # 真合并到 capabilities.json

设计要点：
- 备份当前 capabilities.json 为 capabilities.revN.json.bak
- revision +1
- 写每个 model×thinking 组合的 entry（5 档 glm-5.3-flash = 5 个新 entry）
- capabilities 子字段从 case.score 聚合（按 case.dimension 分组算 mean）
- runtime/cost 暂时 null（后续 update_capabilities.py 跑分填）
- 只 verdict=real 的 case 参与聚合（verdict=empty/failed 跳过）
"""
import argparse
import json
import shutil
import sys
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

COUNCIL_DIR = Path(__file__).resolve().parent
CAPS_FILE = COUNCIL_DIR / "capabilities.json"
SCORES_DIR = COUNCIL_DIR / "benchmark" / "scores"
RESPONSES_DIR = COUNCIL_DIR / "benchmark" / "responses"
V21_CASES = COUNCIL_DIR / "benchmark" / "v21-cases.json"

# glm-5.3-flash 5 档元数据（vendorGroup + provider 从之前 bridge 文件里已知）
GLM_FLASH_THINKING = ["minimal", "low", "medium", "high", "max"]
# tier 分类：T1-pay-per-token（跟 deepseek-v4-pro 一样 — Lite 套餐按 token 算）


def load_v21_cases():
    """读 v21-cases.json，提取 case dimension 映射（case_id -> dimension）"""
    cases_data = json.loads(V21_CASES.read_text(encoding="utf-8"))
    cases = cases_data if isinstance(cases_data, list) else cases_data.get("cases", [])
    return {c["id"]: c for c in cases}


def aggregate_scores_for_model_thinking(model: str, thinking: str, case_dim: dict):
    """聚合 benchmark/scores/{model}__{thinking}/ 下的所有 verdict=real case。

    Returns: dict {dimension: {score: float, samples: int, _source_run_ids: [...]}}
    """
    score_dir = SCORES_DIR / f"{model}__{thinking}"
    if not score_dir.exists():
        return {}, 0

    # dimension -> [score, ...]
    dim_to_scores = defaultdict(list)
    dim_to_case_ids = defaultdict(list)
    total_real = 0
    for score_file in score_dir.glob("*.json"):
        try:
            rec = json.loads(score_file.read_text(encoding="utf-8"))
        except Exception:
            continue
        if rec.get("verdict") != "real" or rec.get("score") is None:
            continue
        case_id = rec.get("case_id", score_file.stem)
        dimension = case_dim.get(case_id, {}).get("dimension", "unknown")
        dim_to_scores[dimension].append(rec["score"])
        dim_to_case_ids[dimension].append(case_id)
        total_real += 1

    # 转成 capabilities schema
    out = {}
    for dim, scores in dim_to_scores.items():
        out[dim] = {
            "score": round(sum(scores) / len(scores), 2),
            "samples": len(scores),
            "freshness": 1.0,
            "interpolated": False,
            "_source_run_ids": sorted(set(dim_to_case_ids[dim])),
        }
    return out, total_real


def build_glm_flash_entries(case_dim: dict):
    """为 glm-5.3-flash 5 档构建 capabilities entries。"""
    entries = {}
    total_real_cases = 0
    for thinking in GLM_FLASH_THINKING:
        caps, real_count = aggregate_scores_for_model_thinking("glm-5.3-flash", thinking, case_dim)
        total_real_cases += real_count
        entry_key = f"glm-5.3-flash__{thinking}"
        entries[entry_key] = {
            "baseModel": "glm-5.3-flash",
            "thinking": thinking,
            "provider": "zai-coding-cn",
            "vendorGroup": "zai",
            "tier": "T1-pay-per-token",
            "stable": True,
            "identityUnknown": False,
            "capabilities": caps,
            "runtime": {
                "avgVerifyScore": None,
                "successRate": None,
                "samples": 0,
            },
            "cost": {
                "avgInputTokens": None,
                "avgOutputTokens": None,
                "costPerCallCny": None,
            },
        }
    return entries, total_real_cases


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--apply", action="store_true", help="真写 capabilities.json（默认 dry-run）")
    args = ap.parse_args()

    if not CAPS_FILE.exists():
        print(f"ERROR: {CAPS_FILE} not found", file=sys.stderr)
        sys.exit(1)

    case_dim = load_v21_cases()
    print(f"loaded {len(case_dim)} v21 cases")

    new_entries, total_real = build_glm_flash_entries(case_dim)
    print(f"\nbuilt {len(new_entries)} glm-5.3-flash entries ({total_real} total verdict=real cases)")
    for key, entry in new_entries.items():
        caps_count = len(entry["capabilities"])
        avg_score = (sum(c["score"] for c in entry["capabilities"].values()) / caps_count) if caps_count > 0 else 0
        print(f"  {key:35}  dimensions={caps_count}  avg_score={avg_score:.2f}")

    caps = json.loads(CAPS_FILE.read_text(encoding="utf-8"))
    print(f"\ncurrent capabilities.json: {len(caps['models'])} models, revision={caps.get('revision', 0)}")

    if not args.apply:
        print("\n[dry-run] pass --apply to actually write capabilities.json")
        return

    # 备份
    old_rev = caps.get("revision", 0)
    backup = COUNCIL_DIR / f"capabilities.rev{old_rev}.json.bak"
    if not backup.exists():
        shutil.copy2(CAPS_FILE, backup)
        print(f"backup → {backup.name}")
    else:
        print(f"backup {backup.name} already exists, skipping")

    # 合并新 entries
    for key, entry in new_entries.items():
        caps["models"][key] = entry

    # bump revision + generatedAt
    caps["revision"] = old_rev + 1
    caps["generatedAt"] = datetime.now(timezone.utc).isoformat(timespec="seconds")

    # 写
    CAPS_FILE.write_text(json.dumps(caps, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\n[applied] capabilities.json: {len(caps['models'])} models, revision={caps['revision']}")


if __name__ == "__main__":
    main()