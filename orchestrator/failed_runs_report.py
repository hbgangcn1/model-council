"""failed_runs 处置链路（v15.5 问题5）：读 failed_runs.log 按 errorCode 分类，
产出处置建议（修复类/重试类/人工类）。供插件每日告警与 council_status 消费。

用法：python orchestrator/failed_runs_report.py [--days 7]
退出码：0=正常（含告警摘要）。
"""
import json
import re
import sys
from pathlib import Path
from datetime import datetime, timedelta, timezone

BASE = Path(__file__).resolve().parent.parent
LOG = BASE / "failed_runs.log"
OUT = BASE / "failed-runs-report.json"

SHANGHAI = timezone(timedelta(hours=8))

# errorCode → (类别, 建议动作)
CLASSIFY = {
    "timeout": ("重试类", "检查任务复杂度；超时常见于思考重档——降低档位或等 wallBudget 动态预算生效"),
    "no_interpreter": ("修复类", "python 解释器不可用——检查 python 环境变量"),
    "council_error": ("修复类", "orchestrator 内部错误——读 run 目录 result.json 诊断"),
    "exit_1": ("人工类", "子进程非零退出——读 result.json 与 rounds.jsonl 定位"),
}


def parse_line(line: str) -> dict:
    try:
        obj = json.loads(line)
        return obj
    except json.JSONDecodeError:
        return {"raw": line[:200]}


def main(days: int = 7):
    if not LOG.exists():
        return {"failedRuns": 0, "note": "无失败记录"}
    cutoff = datetime.now(SHANGHAI) - timedelta(days=days)
    rows = []
    resolved = 0
    for line in LOG.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        obj = parse_line(line)
        # v15.5c（自评测 2026-08-26_20-59-21）：已处置历史（带 resolution/resolvedAt）
        # 不计入「当前故障」视图，保留在日志中供审计，统计单独列出。
        if obj.get("resolution") or obj.get("resolvedAt"):
            resolved += 1
            continue
        ts = obj.get("ts") or obj.get("time") or obj.get("errorAt")
        if ts:
            try:
                dt = datetime.fromisoformat(str(ts))
                if dt.tzinfo is None:
                    dt = dt.replace(tzinfo=SHANGHAI)
                if dt < cutoff:
                    continue
            except ValueError:
                pass
        rows.append(obj)
    by_code = {}
    for obj in rows:
        # v15.5c（元评审 2026-08-26 K8）：分组 key 含 script——纯 errorCode 会把
        # 不同脚本的同名退出码（如 auto_evolve 与 cost_calibrate 的 exit_2）混为一类。
        script = str(obj.get("script") or "")
        code = str(obj.get("errorCode") or obj.get("code") or "unknown")
        key = f"{script}::{code}" if script else code
        by_code.setdefault(key, []).append(obj)
    summary = []
    for key, items in sorted(by_code.items(), key=lambda kv: -len(kv[1])):
        script, _, code = key.rpartition("::")
        cls, advice = CLASSIFY.get(code, ("人工类", "未分类错误码——人工检查"))
        summary.append({"errorCode": code, "script": script or None, "count": len(items),
                        "category": cls, "advice": advice,
                        "last": items[-1].get("ts") or items[-1].get("raw", "")[:80]})
    out = {"generatedAt": datetime.now(SHANGHAI).isoformat(),
           "windowDays": days, "failedRuns": len(rows), "resolvedHistory": resolved,
           "byErrorCode": summary,
           "note": "处置链路：修复类→修代码；重试类→自动重试/降档；人工类→council_status 告警待人工；resolvedHistory=窗口内已处置历史（不计入当前故障）"}
    tmp = OUT.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(out, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(OUT)
    return out


if __name__ == "__main__":
    days = 7
    if "--days" in sys.argv:
        days = int(sys.argv[sys.argv.index("--days") + 1])
    result = main(days)
    print(json.dumps(result, ensure_ascii=False, indent=2))
