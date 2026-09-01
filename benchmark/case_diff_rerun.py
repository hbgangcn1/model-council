"""v15.4：考卷换题检测 + 内容 diff 增量重跑（§14-v15.4-F-28）。

每日 02:00 定时调用（插件）：逐题比对 v21-cases.json 的内容哈希与
benchmark/cases-state.json 记录——只重跑「内容变化题 + 新题」的所有候选；
变化超过 50% 自动升级为全量（--fresh）。成绩已绑定 caseHash，旧内容产物自动作废。

用法：
  python benchmark/case_diff_rerun.py            # 检测+重跑（无变化则 no-op）
  python benchmark/case_diff_rerun.py --dry      # 只报告不跑
"""
import argparse
import hashlib
import json
import subprocess
import sys
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent  # council/
V21 = BASE / "benchmark" / "v21-cases.json"
STATE = BASE / "benchmark" / "cases-state.json"
RUNNER = BASE / "benchmark" / "bench" / "runner.py"


def _case_hash(case: dict) -> str:
    canonical = json.dumps(case, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:16]


def detect() -> dict:
    """→ {changed: [ids], added: [ids], removed: [ids], totalChangedPct, state}"""
    doc = json.loads(V21.read_text(encoding="utf-8"))
    cases = doc.get("cases", [])
    prev = {}
    if STATE.exists():
        try:
            prev = {k: v for k, v in
                    json.loads(STATE.read_text(encoding="utf-8")).get("cases", {}).items()}
        except (json.JSONDecodeError, OSError):
            prev = {}
    current = {c["id"]: _case_hash(c) for c in cases}
    changed = [cid for cid, h in current.items() if cid in prev and prev[cid] != h]
    added = [cid for cid in current if cid not in prev]
    removed = [cid for cid in prev if cid not in current]
    total = max(len(current), 1)
    return {"changed": changed, "added": added, "removed": removed,
            "totalChangedPct": round((len(changed) + len(added) + len(removed)) / total * 100, 1),
            "state": {"cases": current, "contentHash": doc.get("contentHash"),
                      "totalCases": len(cases), "ts": None}}


def rerun(dry: bool = False) -> dict:
    d = detect()
    changed_ids = sorted(set(d["changed"]) | set(d["added"]))
    n_total = len(d["state"]["cases"])
    if not changed_ids:
        return {**d, "action": "noop"}
    # >50% 变化 → 全量重跑
    if (len(changed_ids) / max(n_total, 1)) > 0.5:
        action = "full"
        cmd = [sys.executable, str(RUNNER), "--fresh"]
    else:
        action = "diff"
        cmd = [sys.executable, str(RUNNER), "--cases", ",".join(changed_ids)]
    if not dry:
        result = subprocess.run(cmd, cwd=str(BASE), capture_output=True, text=True, timeout=7200)
        if result.returncode != 0:
            return {**d, "action": action, "ok": False,
                    "exitCode": result.returncode,
                    "stderrTail": (result.stderr or "")[-400:]}
        import datetime
        d["state"]["ts"] = datetime.datetime.now().astimezone().isoformat()
        tmp = STATE.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(d["state"], ensure_ascii=False, indent=2), encoding="utf-8")
        tmp.replace(STATE)
    return {**d, "action": action, "ok": True, "dry": dry}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry", action="store_true")
    args = ap.parse_args()
    print(json.dumps(rerun(dry=args.dry), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
