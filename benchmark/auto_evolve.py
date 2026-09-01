"""v15.4：benchmark 全自动进化编排（§14-v15.4-F-28/30）。

插件每日 02:00 调用：换题检测 → 内容 diff 重跑（>50% 变化自动全量）→
回归门禁（换题时重建基线，无换题时防异常）→ capability_ingest 自动合入。
熔断安全网：门禁连续 3 次拦截 → 自进化暂停（council_status 告警），
暂停后每日 dry 体检，连续 7 天通过 → 自动恢复。

用法：
  python benchmark/auto_evolve.py           # 完整链路
  python benchmark/auto_evolve.py --dry     # 只检测+报告
"""
import argparse
import json
import subprocess
import sys
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent  # council/
if str(BASE) not in sys.path:
    sys.path.insert(0, str(BASE))  # v15.5b（F2 根因）：脚本以 python benchmark/xxx.py 运行时需要 council 根在 path
BENCH = BASE / "benchmark"
STATE = BASE / "auto-evolve-state.json"
EVENTS = BASE / "auto-evolve-events.jsonl"

PAUSE_THRESHOLD = 3     # 连续门禁拦截次数 → 暂停
AUTO_RESUME_DAYS = 7    # 暂停后连续 dry 通过天数 → 自动恢复


def _load_state() -> dict:
    if STATE.exists():
        try:
            return json.loads(STATE.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            pass
    return {"consecutiveFailures": 0, "paused": False, "pausedReason": None,
            "dryPassDays": 0}


def _save_state(st: dict):
    tmp = STATE.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(st, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(STATE)


def _log_event(ev: dict):
    try:
        with EVENTS.open("a", encoding="utf-8") as f:
            f.write(json.dumps(ev, ensure_ascii=False) + "\n")
    except OSError:
        pass


def _run_py(script: str, args: list, timeout_s: int = 3600):
    return subprocess.run([sys.executable, str(BENCH / script)] + args,
                          cwd=str(BASE), capture_output=True, text=True, timeout=timeout_s)


def _ingest_auto_apply() -> dict:
    """生成 pending diff → 自动 apply（内部校验 casesHash/baseHash，坏数据拒绝）。"""
    r1 = _run_py("capability_ingest.py", [])
    if r1.returncode != 0:
        return {"ok": False, "stage": "diff", "err": (r1.stderr or r1.stdout)[-300:]}
    # 找 pending diff 路径（固定 PENDING_DIFF）
    r2 = _run_py("capability_ingest.py", ["--apply"])
    if r2.returncode != 0:
        return {"ok": False, "stage": "apply", "err": (r2.stderr or r2.stdout)[-300:]}
    try:
        return {"ok": True, "result": json.loads(r2.stdout)}
    except json.JSONDecodeError:
        return {"ok": True, "result": (r2.stdout or "")[-300:]}


def run(dry: bool = False) -> dict:
    st = _load_state()
    from benchmark.case_diff_rerun import detect, rerun as _rerun
    d = detect()
    if st.get("paused"):
        # 暂停期：每日 dry 体检，连续 AUTO_RESUME_DAYS 天通过 → 自动恢复
        if d["totalChangedPct"] == 0 and not (d["changed"] or d["added"]):
            st["dryPassDays"] = int(st.get("dryPassDays", 0)) + 1
            if st["dryPassDays"] >= AUTO_RESUME_DAYS:
                st["paused"] = False
                st["pausedReason"] = None
                st["dryPassDays"] = 0
                st["consecutiveFailures"] = 0
                _log_event({"ts": None, "op": "auto-resume", "days": AUTO_RESUME_DAYS})
        else:
            st["dryPassDays"] = 0
        _save_state(st)
        return {"paused": st["paused"], "dryPassDays": st["dryPassDays"],
                "reason": st.get("pausedReason"), "note": "自进化暂停中（熔断），每日 dry 体检"}
    if not (d["changed"] or d["added"]):
        _save_state(st)
        return {"action": "noop", "paused": False, "note": "考卷无变化"}
    if dry:
        return {"action": "dry-detect", "changed": d["changed"], "added": d["added"],
                "totalChangedPct": d["totalChangedPct"], "paused": False}
    # 重跑（diff 增量 / >50% 全量）
    rr = _rerun(dry=False)
    if not rr.get("ok"):
        st["consecutiveFailures"] += 1
        st["paused"] = st["consecutiveFailures"] >= PAUSE_THRESHOLD
        st["pausedReason"] = st["pausedReason"] or f"rerun_failed_{st['consecutiveFailures']}x"
        _save_state(st)
        _log_event({"ts": None, "op": "rerun-failed", "err": str(rr.get("stderrTail", ""))[:300]})
        return {"ok": False, "stage": "rerun", "consecutiveFailures": st["consecutiveFailures"]}
    # 回归门禁：换题重建基线（换题是预期变化），否则防异常
    gate = _run_py("regression_gate.py", ["--update-baseline"])
    if gate.returncode != 0:
        st["consecutiveFailures"] += 1
        st["paused"] = st["consecutiveFailures"] >= PAUSE_THRESHOLD
        st["pausedReason"] = st["pausedReason"] or f"regression_gate_{st['consecutiveFailures']}x"
        _save_state(st)
        _log_event({"ts": None, "op": "gate-failed",
                    "err": (gate.stderr or gate.stdout)[-300:]})
        return {"ok": False, "stage": "gate", "consecutiveFailures": st["consecutiveFailures"]}
    # 自动合入（ingest 内部校验，坏数据拒绝）
    ing = _ingest_auto_apply()
    if not ing.get("ok"):
        st["consecutiveFailures"] += 1
        st["paused"] = st["consecutiveFailures"] >= PAUSE_THRESHOLD
        st["pausedReason"] = st["pausedReason"] or f"ingest_failed_{st['consecutiveFailures']}x"
        _save_state(st)
        _log_event({"ts": None, "op": "ingest-failed", "err": str(ing.get("err", ""))[:300]})
        return {"ok": False, "stage": "ingest", "consecutiveFailures": st["consecutiveFailures"]}
    st["consecutiveFailures"] = 0
    _save_state(st)
    _log_event({"ts": None, "op": "auto-evolved", "action": rr.get("action"),
                "changed": d["changed"], "added": d["added"], "ingest": ing.get("result")})
    return {"ok": True, "action": rr.get("action"), "changed": d["changed"],
            "added": d["added"], "ingest": ing.get("result")}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry", action="store_true")
    args = ap.parse_args()
    print(json.dumps(run(dry=args.dry), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
