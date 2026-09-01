"""H3：成本对账（cost_calibrate）。

扫描 runs/*/cost.jsonl：每笔调用带 estCostCny（对账口径估算）与 actualCostCny（真实 usage 计价，
缓存命中已按 cacheInputCnyPerMTok 折算），7 日窗口汇总 drift：
  drift_pct = (Σactual − Σest) / Σest × 100
输出 cost-drift.json，供 /api/council/state、council_status 与 Prometheus 指标消费。

v15.2（P0-3）：新增按 model×role 的因子拆分（est/actual/行数），
--check 模式：|drift| > reconcileAlertPct → 写 cost-reconcile-events.jsonl + 退出码 2（告警），
供插件每日 04:00 定时对账。

v15.3（元评审 P0-3 校准闭环）：--check 与 --recalibrate 会把 byModelRole 的
actual/est 比值 EWMA 回填 token-profiles.json 的 per-(model,role) calib 系数
（n≥5 才更新），估算路径乘该系数后 drift 逐步收敛——此前 drift -15.87% 是
单向系统性高估（verifier est_out 硬编码 500 vs 实测 460、synthesize 1500 vs 实测 3721），
只告警不校准则永远漂。

用法：
  python orchestrator/cost_calibrate.py           # 计算并落盘
  python orchestrator/cost_calibrate.py --json    # 只打印结果
  python orchestrator/cost_calibrate.py --check   # 计算 + 超阈值告警 + 校准回填（退出码 2）
  python orchestrator/cost_calibrate.py --recalibrate  # 仅执行校准回填（不告警）
"""
import json
import sys
import time
from pathlib import Path

try:
    from .config_loader import now_shanghai
    from . import params as params_mod
    from . import token_profiles
except ImportError:
    from config_loader import now_shanghai  # type: ignore
    import params as params_mod  # type: ignore
    import token_profiles  # type: ignore

BASE = Path(__file__).resolve().parent.parent  # council/
RUNS = BASE / "runs"
OUT = BASE / "cost-drift.json"
EVENTS = BASE / "cost-reconcile-events.jsonl"
WINDOW_DAYS = 7


def scan_runs(runs_dir: Path = RUNS, window_days: int = WINDOW_DAYS,
              now_s: float = None) -> list:
    """→ [{run, estCny, actualCny, pricedRows, estRows}]（按 run 目录 mtime 降序，7 日窗口）。"""
    now_s = now_s if now_s is not None else time.time()
    cutoff = now_s - window_days * 86400
    rows = []
    for run_dir in runs_dir.iterdir():
        if not run_dir.is_dir():
            continue
        try:
            if run_dir.stat().st_mtime < cutoff:
                continue
        except OSError:
            continue
        est = 0.0
        actual = 0.0
        priced = 0
        est_rows = 0
        cost_file = run_dir / "cost.jsonl"
        if not cost_file.exists():
            continue
        for line in cost_file.read_text(encoding="utf-8").splitlines():
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                continue
            e = rec.get("estCostCny")
            a = rec.get("actualCostCny")
            if isinstance(e, (int, float)):
                est += e
                est_rows += 1
            if isinstance(a, (int, float)):
                actual += a
                priced += 1
        rows.append({"run": run_dir.name, "estCny": round(est, 6),
                     "actualCny": round(actual, 6), "pricedRows": priced,
                     "estRows": est_rows, "mtime": run_dir.stat().st_mtime})
    # M3 同类修复：run 目录名混用两种格式（20260506-192100 / 2026-08-24_02-01-00），
    # 字符串排序会把旧格式排前面 → 改按 mtime 降序（与宿主 recentRunDirs 同口径）。
    rows.sort(key=lambda r: r["mtime"], reverse=True)
    for r in rows:
        r.pop("mtime", None)
    return rows


def _breakdown(rows_dir: Path = RUNS, window_days: int = WINDOW_DAYS) -> dict:
    """P0-3：按 model×role 拆分 est/actual，定位漂移来源。"""
    now_s = time.time()
    cutoff = now_s - window_days * 86400
    agg = {}
    for run_dir in rows_dir.iterdir():
        if not run_dir.is_dir():
            continue
        try:
            if run_dir.stat().st_mtime < cutoff:
                continue
        except OSError:
            continue
        cost_file = run_dir / "cost.jsonl"
        if not cost_file.exists():
            continue
        for line in cost_file.read_text(encoding="utf-8").splitlines():
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                continue
            e = rec.get("estCostCny")
            a = rec.get("actualCostCny")
            if not isinstance(e, (int, float)) or not isinstance(a, (int, float)):
                continue
            key = f"{rec.get('model', '?')}::{rec.get('role', '?')}"
            cell = agg.setdefault(key, {"est": 0.0, "actual": 0.0, "n": 0})
            cell["est"] += e
            cell["actual"] += a
            cell["n"] += 1
    return {k: {**v, "est": round(v["est"], 6), "actual": round(v["actual"], 6),
                "driftPct": round((v["actual"] - v["est"]) / v["est"] * 100, 1) if v["est"] else None}
            for k, v in sorted(agg.items(), key=lambda kv: kv[1]["est"], reverse=True)}


def calibrate(runs_dir: Path = RUNS, out_path: Path = OUT,
              window_days: int = WINDOW_DAYS, write: bool = True) -> dict:
    rows = scan_runs(runs_dir, window_days)
    est_total = sum(r["estCny"] for r in rows)
    actual_total = sum(r["actualCny"] for r in rows)
    priced_rows = sum(r["pricedRows"] for r in rows)
    drift = None
    if est_total > 0 and priced_rows > 0:
        drift = round((actual_total - est_total) / est_total * 100, 2)
    out = {"generatedAt": now_shanghai().isoformat(), "windowDays": window_days,
           "runs": len(rows), "estCny": round(est_total, 6),
           "actualCny": round(actual_total, 6), "pricedRows": priced_rows,
           "driftPct": drift, "perRun": rows, "byModelRole": _breakdown(runs_dir, window_days),
           "note": "driftPct 仅在 pricedRows>0 时有效；est=对账口径估算（与 actual 同构，P0-3）"}
    if write:
        tmp = out_path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(out, ensure_ascii=False, indent=2), encoding="utf-8")
        tmp.replace(out_path)
    return out


def recalibrate(runs_dir: Path = RUNS, window_days: int = WINDOW_DAYS) -> dict:
    """v15.3：按 byModelRole 的 actual/est 比值回填 token-profiles.json 校准系数。
    返回 {key: {ratio, n, applied}}。n < CALIB_MIN_N 的 cell 跳过（样本不足不校准）。"""
    breakdown = _breakdown(runs_dir, window_days)
    out = {}
    for key, cell in breakdown.items():
        est, actual, n = cell["est"], cell["actual"], cell["n"]
        if not est or n < token_profiles.CALIB_MIN_N:
            continue
        ratio = actual / est
        if "::" not in key:
            continue
        model, role = key.split("::", 1)
        token_profiles.update_calib(model, role, ratio, n=n)
        out[key] = {"ratio": round(ratio, 4), "n": n, "applied": True}
    return out


def check(alert_pct: float = None) -> tuple:
    """对账 + 阈值告警。返回 (out, alert)。alert=true 时写 events 并建议退出码 2。
    v15.5（问题 4）：drift 超 ±10% 线时**强制自动校准**（check 内已回填 recalibrate，
    保证超线后 24h 内（下次每日对账前）系数已修正）；out 带 autoCalibrated 标记。"""
    if alert_pct is None:
        alert_pct = float(params_mod.load().get("cost", {}).get("reconcileAlertPct", 10.0))
    out = calibrate()
    calib = recalibrate()
    out["calibrated"] = calib
    drift = out.get("driftPct")
    alert = drift is not None and abs(drift) > alert_pct
    out["autoCalibrated"] = bool(calib) and alert
    if alert:
        ev = {"ts": out["generatedAt"], "driftPct": drift,
              "thresholdPct": alert_pct, "windowDays": out["windowDays"],
              "estCny": out["estCny"], "actualCny": out["actualCny"],
              "calibrated": calib,
              "autoCalibrated": bool(calib),
              "note": "v15.5：超线自动校准已执行（估算系数回填）"}
        try:
            with EVENTS.open("a", encoding="utf-8") as f:
                f.write(json.dumps(ev, ensure_ascii=False) + "\n")
        except OSError:
            pass
    return out, alert


if __name__ == "__main__":
    if "--check" in sys.argv:
        result, alerted = check()
        print(json.dumps({**result, "alerted": alerted}, ensure_ascii=False, indent=2))
        sys.exit(2 if alerted else 0)
    if "--recalibrate" in sys.argv:
        result = recalibrate()
        print(json.dumps(result, ensure_ascii=False, indent=2))
        sys.exit(0)
    result = calibrate()
    print(json.dumps(result, ensure_ascii=False, indent=2))
