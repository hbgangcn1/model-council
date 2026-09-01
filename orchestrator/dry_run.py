"""dry-run 回放：历史任务 → 动态分配 vs 旧静态表对比。"""
import datetime
import json
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from orchestrator import selector

BASE = Path(__file__).resolve().parent.parent
RUNS = BASE / "runs"

def task_vector_of(text: str) -> dict:
    """启发式：从任务描述推断维度权重（与 decomposer 的 weightVector 对齐）。"""
    low = text.lower()
    v = {"reasoning": 0.2, "code": 0.1, "chinese": 0.2, "research": 0.2,
         "instruction_following": 0.1, "long_context": 0.1,
         "tool_use": 0.0, "creativity": 0.05, "safety": 0.05}
    if re.search(r"代码|架构|技术选型|重构|bug|编程|开发|插件|api", low):
        v = {**v, "code": 0.4, "reasoning": 0.25, "chinese": 0.1, "research": 0.1}
    if re.search(r"市场|调研|分析报告|行业|趋势|竞品|商业计划", low):
        v = {**v, "research": 0.45, "chinese": 0.25, "reasoning": 0.15}
    if re.search(r"定价|财务|成本|投资|金融", low):
        v = {**v, "reasoning": 0.3, "research": 0.3, "chinese": 0.15}
    s = sum(v.values())
    return {k: round(x / s, 3) for k, x in v.items()}

def load_briefs(limit: int = 10):
    briefs = []
    # M3：run 目录名混用两种格式（20260506-192100 / 2026-08-24_02-01-00），
    # 字符串排序会把 '0' 排在 '-' 前导致新 run 静默回退旧数据 → 必须按 mtime 排序。
    dirs = sorted(
        (d for d in RUNS.iterdir() if d.is_dir()),
        key=lambda d: d.stat().st_mtime,
        reverse=True)
    for run_dir in dirs:
        for f in sorted(run_dir.glob("brief*.md")):
            text = f.read_text(encoding="utf-8", errors="replace")
            briefs.append({"run": run_dir.name, "file": f.name, "text": text})
        if len(briefs) >= limit:
            break
    return briefs[:limit]

def load_old_presets():
    """旧静态表：presets.json 的 balanced preset 角色映射。"""
    p = BASE / "presets.json"
    if not p.exists():
        return {}
    data = json.loads(p.read_text(encoding="utf-8"))
    return data.get("presets", {}).get("balanced", {}).get("roles", {})

def main():
    caps = selector.load_capabilities()
    if not caps.get("models"):
        print("❌ capabilities.json 尚无模型数据（基准未完成），无法 dry-run")
        sys.exit(1)
    briefs = load_briefs()
    old = load_old_presets()
    now = datetime.datetime.now()
    results = []
    for b in briefs:
        tv = task_vector_of(b["text"])
        ctx = {"lambda_": 0.5, "mu": 0, "used_providers": set(),
               "banned_models": set(), "balance": {}, "epsilon": 0.0,
               "est_input_tokens": selector.estimate_tokens(b["text"]) + 200,
               "est_output_tokens": 600, "now": now}
        ranked = selector.select(tv, ctx, caps)
        top = [(cid, s) for cid, cand, s, cost in ranked[:3] if s is not None]
        results.append({"run": b["run"], "file": b["file"],
                        "taskVector": tv, "top3": top})
        print(f"\n=== {b['run']} / {b['file']} ===")
        print(f"  weightVector: {tv}")
        for cid, s in top:
            print(f"    {cid}: {s}")
    print(f"\n=== 旧静态表（balanced preset）===")
    for role, model in old.items():
        print(f"  {role}: {model}")
    out = BASE / "dry-run-results.json"
    out.write_text(json.dumps({"generatedAt": now.isoformat(), "results": results,
                               "old_presets_balanced": old},
                              ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\n✅ 结果写入 {out}")

if __name__ == "__main__":
    main()
