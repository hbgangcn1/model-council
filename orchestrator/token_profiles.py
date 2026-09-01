"""P0-3：token 用量档案（用真实 usage 的 EWMA 取代硬编码 est_out/est_in）。

成本漂移证据（2026-08-24 元评审 + scratch/cost-drift-evidence.md）：
输出 token 硬编码（exec=800/verifier=500/synthesize=1500）是双向漂移主因
（19-13-33 合成行 est 1500 vs 实际 7920 → 低估 37%；19-07-23 反向高估 2 倍）。

方案：token-profiles.json 按 (model, role) 记录输入/输出 token 的 EWMA，
每次 run 收尾用真实 usage 更新；est_for() 优先取档案值，冷启动回退启发式。
cacheHitRate 同样按全局 EWMA 维护，供 selector 缓存建模。
"""
import json
import os
import sys
import threading
import time
from pathlib import Path

try:
    from .config_loader import now_shanghai
except ImportError:
    from config_loader import now_shanghai  # type: ignore

BASE = Path(__file__).resolve().parent.parent  # council/
OUT = Path(os.environ.get("COUNCIL_TOKEN_PROFILES_FILE", str(BASE / "token-profiles.json")))

# v15.3：写盘并发加固（exec/verifier 多线程同时 record，tmp+replace 会撞 WinError 32，
# 与 selector._write_circuit 同款问题——2026-08-24 实测炸 run）。线程锁 + 退避重试。
_prof_lock = threading.Lock()


def _atomic_write(prof: dict):
    with _prof_lock:
        for attempt in range(6):
            try:
                tmp = OUT.with_suffix(".json.tmp")
                tmp.write_text(json.dumps(prof, ensure_ascii=False, indent=2), encoding="utf-8")
                tmp.replace(OUT)
                return
            except OSError as e:
                if attempt == 5:
                    print(f"[council] token-profiles 写入失败（放弃本次）：{e}", file=sys.stderr)
                    return
                time.sleep(0.05 * (attempt + 1))

EWMA_ALPHA = 0.3          # 新样本权重
MIN_SAMPLES = 3           # 少于该样本数不启用档案值（防单次长文带偏）
DEFAULT_OUT = {"exec": 800, "verifier": 500, "synthesize": 1500, "decompose": 800}
DEFAULT_IN_PAD = {"exec": 400, "verifier": 800, "synthesize": 400, "decompose": 400}

# v15.3（元评审 P0-3 成本校准闭环）：per-(model, role) 成本校准系数。
# cost_calibrate.py 每次对账（--check/--recalibrate）把 byModelRole 的 actual/est 比值
# EWMA 回填到此（update_calib），估算路径（_cost_pair/effective_cost_cny）乘该系数，
# 让 drift 逐步收敛。钳制 [0.5, 2.0] 防单次异常值带偏；样本数不足（calibN < 5）不启用。
CALIB_CLAMP = (0.5, 2.0)
CALIB_MIN_N = 5
CALIB_ALPHA = 0.3         # 校准比值的新样本权重（0.3 实际 / 0.7 历史，阻尼防震荡）


def load():
    if not OUT.exists():
        return {"updatedAt": None, "byModel": {}, "cacheHitRate": 0.0, "n": 0}
    try:
        return json.loads(OUT.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return {"updatedAt": None, "byModel": {}, "cacheHitRate": 0.0, "n": 0}


def est_for(model: str, role: str, fallback_in: int, fallback_out: int):
    """→ (est_in, est_out)。档案样本充足时用 EWMA 值，否则用启发式回退。"""
    prof = load()
    m = (prof.get("byModel") or {}).get(model)
    r = (m or {}).get(role) if m else None
    if r and r.get("n", 0) >= MIN_SAMPLES:
        return max(1, int(r["avgIn"])), max(1, int(r["avgOut"]))
    return fallback_in, fallback_out


def record(model: str, role: str, prompt_tokens, completion_tokens, cache_hit_tokens=0):
    """用一笔真实 usage 更新 EWMA。原子写。"""
    prof = load()
    by_model = prof.setdefault("byModel", {})  # 必须 setdefault：`or {}` 会创建游离字典，写不进 prof
    m = by_model.setdefault(model, {})
    r = m.get(role)
    pin = int(prompt_tokens or 0)
    pout = int(completion_tokens or 0)
    if pin <= 0 and pout <= 0:
        return prof  # 无效用量不更新
    if r is None:
        r = m[role] = {"avgIn": float(pin), "avgOut": float(pout), "n": 1}
    else:
        n = int(r.get("n", 0))
        r["avgIn"] = round(EWMA_ALPHA * pin + (1 - EWMA_ALPHA) * float(r.get("avgIn", pin)), 1)
        r["avgOut"] = round(EWMA_ALPHA * pout + (1 - EWMA_ALPHA) * float(r.get("avgOut", pout)), 1)
        r["n"] = n + 1
    # 缓存命中率：全局 EWMA（命中/总输入）
    prof["n"] = int(prof.get("n", 0)) + 1
    if pin + (cache_hit_tokens or 0) > 0:
        hit_ratio = min(1.0, (cache_hit_tokens or 0) / max(pin + (cache_hit_tokens or 0), 1))
        prof["cacheHitRate"] = round(
            EWMA_ALPHA * hit_ratio + (1 - EWMA_ALPHA) * float(prof.get("cacheHitRate", 0.0)), 4)
    prof["updatedAt"] = now_shanghai().isoformat()
    _atomic_write(prof)
    return prof


def cache_hit_rate():
    prof = load()
    try:
        return max(0.0, min(0.9, float(prof.get("cacheHitRate", 0.0))))
    except (TypeError, ValueError):
        return 0.0


def calib_for(model: str, role: str = None) -> float:
    """v15.3：per-(model, role) 成本校准系数。role=None 时取该模型各 role 平均（选择时用）。
    样本不足 CALIB_MIN_N 不启用（回退 1.0 = 不校准）。"""
    prof = load()
    m = (prof.get("byModel") or {}).get(model)
    if not isinstance(m, dict):
        return 1.0
    cells = []
    if role:
        r = m.get(role)
        if isinstance(r, dict):
            cells = [r]
    else:
        cells = [v for v in m.values() if isinstance(v, dict)]
    vals = []
    for c in cells:
        cb = c.get("calib")
        cn = c.get("calibN", 0)
        if isinstance(cb, (int, float)) and int(cn) >= CALIB_MIN_N:
            vals.append(float(cb))
    return round(sum(vals) / len(vals), 4) if vals else 1.0


def update_calib(model: str, role: str, ratio: float, n: int = 0, alpha: float = None) -> dict:
    """v15.3：把对账实测 actual/est 比值 EWMA 回填校准系数。原子写，返回档案。
    ratio 非法（≤0 / NaN / inf）忽略；钳制 [0.5, 2.0]。
    n = 本次对账窗口内该 (model, role) 的样本行数，写进 calibN 供 calib_for 做启用判断
    （样本数而非更新次数——否则每日对账一次要 5 天才启用，闭环太慢）。"""
    if not isinstance(ratio, (int, float)) or ratio <= 0 or ratio != ratio or ratio in (float("inf"), float("-inf")):
        return load()
    a = alpha if alpha is not None else CALIB_ALPHA
    lo, hi = CALIB_CLAMP
    prof = load()
    by_model = prof.setdefault("byModel", {})
    m = by_model.setdefault(model, {})
    r = m.get(role)
    if r is None:
        r = m[role] = {"avgIn": 0.0, "avgOut": 0.0, "n": 0, "calib": 1.0, "calibN": 0}
    old = float(r.get("calib", 1.0))
    new = max(lo, min(hi, a * ratio + (1 - a) * old))
    r["calib"] = round(new, 4)
    r["calibN"] = max(int(r.get("calibN", 0)), int(n or 0))
    prof["updatedAt"] = now_shanghai().isoformat()
    _atomic_write(prof)
    return prof


if __name__ == "__main__":
    prof = load()
    print(json.dumps(prof, ensure_ascii=False, indent=2))
