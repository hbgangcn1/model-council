"""基准完成后的一键收尾：补失败 → 判分 → 汇总 → 生成 ingest 审批 diff → dry-run。

v15.2（2026-08-24 元评审 P0-6）：不再调用 build_capabilities.py 全量覆盖 capabilities.json
（那会清空 runtime/cost 回填与 runtime-feedback 痕迹，与 diff 人工审批流冲突）。
收尾现在只生成 pending-ingest-diff.json；人工审批后 `capability_ingest.py --apply` 才落盘。
"""
import subprocess
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent  # council/
BENCH = BASE / "benchmark"
ORCH = BASE / "orchestrator"

def run(cmd, cwd=None):
    print(f"\n$ {cmd}", flush=True)
    r = subprocess.run(cmd, shell=True, cwd=cwd or str(BASE), capture_output=False)
    if r.returncode != 0:
        print(f"  [exit {r.returncode}]", flush=True)
    return r.returncode

def main():
    runner = str(BENCH / "bench" / "runner.py")
    # 1. 补失败项（新配置：max 档预算上限 + 24576 max_tokens）
    run(f'python "{runner}" --phase all --only-failed')
    # 2. 全量判分 + 汇总（幂等，已完成跳过）
    run(f'python "{runner}" --phase score')
    run(f'python "{runner}" --summary-only')
    # 3. 生成能力档案摄入审批 diff（人工审批后 capability_ingest.py --apply）
    run(f'python "{BENCH / "capability_ingest.py"}"')
    # 4. dry-run 回放
    run(f'python "{ORCH / "dry_run.py"}"')
    print("\n✅ 收尾完成：scores-summary.json + pending-ingest-diff.json + dry-run-results.json 已生成")
    print("   ⚠ capabilities.json 未动：审批 diff 后执行 python benchmark/capability_ingest.py --apply")

if __name__ == "__main__":
    main()
