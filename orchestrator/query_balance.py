"""余额/额度查询（§7.5）：DeepSeek balance + MiniMax token-plan → balance-snapshot.json。60s 缓存。"""
import json
import time
import urllib.request
from pathlib import Path

try:  # 包内导入（council_v14 经 orchestrator 包加载）；直接执行时退回顶层导入
    from . import selector  # norm_percent：百分比语义守卫
except ImportError:
    import selector  # type: ignore

BASE = Path(__file__).resolve().parent.parent  # council/
SNAPSHOT = BASE / "balance-snapshot.json"
CACHE_S = 60

def _get(url, headers):
    req = urllib.request.Request(url, headers=headers, method="GET")
    with urllib.request.urlopen(req, timeout=30) as resp:
        return json.loads(resp.read().decode("utf-8"))

def _deepseek_balance(keys):
    h = {"Authorization": f"Bearer {keys['DEEPSEEK_API_KEY']}"}
    data = _get("https://api.deepseek.com/user/balance", h)
    total = 0.0
    currency = None
    for b in data.get("balance_infos", []):
        total += float(b.get("total_balance", 0))
        currency = b.get("currency") or currency
    return {"deepseek-official:balance": total, "currency": currency,
            "raw_available": data.get("is_available")}

def _minimax_quota(keys):
    h = {"Authorization": f"Bearer {keys['MINIMAX_CN_API_KEY']}"}
    data = _get("https://api.minimaxi.com/v1/token_plan/remains", h)
    out = {}
    for mr in data.get("model_remains", []):
        if mr.get("model_name") != "general":
            continue
        # 实测字段：current_interval_remaining_percent（5h）、
        # current_weekly_remaining_percent（周）。快照统一存整数百分比（96 = 96%），
        # 上游若误传小数比例（0.96）由 norm_percent 自动 ×100 修复（H3 单位契约）。
        out["minimax-cn:5h"] = selector.norm_percent(mr.get("current_interval_remaining_percent", 100))
        out["minimax-cn:week"] = selector.norm_percent(mr.get("current_weekly_remaining_percent", 100))
        break
    return out

def query(force: bool = False) -> dict:
    if not force and SNAPSHOT.exists():
        age = time.time() - SNAPSHOT.stat().st_mtime
        if age < CACHE_S:
            try:
                return json.loads(SNAPSHOT.read_text(encoding="utf-8"))
            except Exception:
                pass
    cred = Path.home() / ".dsh" / ".credentials.yaml"
    keys = {}
    for line in cred.read_text(encoding="utf-8").splitlines():
        parts = line.split(":", 1)
        if len(parts) == 2 and parts[0].strip().isupper():
            keys[parts[0].strip()] = parts[1].strip()
    snap = {"ts": int(time.time() * 1000), "ok": {}, "stale": False, "staleReasons": []}  # ts 统一 epoch 毫秒
    for name, fn in (("deepseek", _deepseek_balance), ("minimax", _minimax_quota)):
        try:
            snap["ok"].update(fn(keys))
        except Exception as e:
            snap.setdefault("errors", {})[name] = str(e)[:200]
    if snap.get("errors"):
        snap["stale"] = True
        snap["staleReasons"] = ["balance_query_failed:" + ",".join(sorted(snap["errors"]))]
    # 月度消耗估算（供 scarcity 计算）：cost-drift 7 日实际 → 日均 × 30
    # （P0-3 修复：此前 self 赋值恒 None 死代码 → quota_factor 对 pay-per-token 恒 1.0）
    monthly = None
    try:
        drift = json.loads((BASE / "cost-drift.json").read_text(encoding="utf-8"))
        runs = drift.get("runs")
        days = max(float(drift.get("windowDays") or 7), 1.0)
        actual = drift.get("actualCny")
        if runs and actual:
            monthly = round(actual / days * 30.0, 4)
    except Exception:
        pass
    snap["deepseek-official:monthly_estimate"] = monthly
    tmp = SNAPSHOT.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(snap, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(SNAPSHOT)
    return snap

if __name__ == "__main__":
    import sys
    print(json.dumps(query(force="--force" in sys.argv), ensure_ascii=False, indent=2))
