"""选择策略：评分函数 + 护栏（优先级明确）+ 熔断器（三态+指数退避）+ ε-greedy
+ token 预估 + 成本函数（表驱动、缓存命中、币种换算）。纯函数模块。

护栏优先级（冲突解决策略，高→低，一次只报最高优先级的 reason_code）：
  1. identity_unknown                   —— 身份未知（无可靠 API 路由）
  2. unstable_member_excluded_from_pool —— 不稳定成员
  3. thinking_not_allowed               —— 档位不允许
  4. circuit_open                       —— 熔断（半开探测见 _probe_begin）
  5. balance_exhausted                  —— 余额/额度耗尽（最高成本风险）
  6. self_verify_ban                    —— 自验禁令

v15.2（2026-08-24 元评审 P0-2/P0-4/P0-3/P2-1/P2-2/P1-3）：
- 静态不合格候选（identityUnknown/stable=false/allowlist/自验）在评分前预过滤，
  每个 select 调用只记一条 pool_excluded 汇总事件（护栏事件量从每轮 ~50 条降到 ~8 条）；
- 延迟项数据源扩展到 caps.runtime.latencyP50Ms（回填后复活，P0-4）；
- effective_cost_cny 增加 baseCostCny（不含 thinking/quota 规划系数）供成本对账同口径比较（P0-3）；
- fx_status() 汇率陈旧黄灯/停机等级（P2-2）；Pareto 前沿软加分（P2-1）；Elo 软惩罚（P1-3）。

v15.4（2026-08-24 the maintainer decided，§14-v15.4）：
- 删除档位→thinking 硬约束（maxThinkingRank）——每档位视作不同模型，selector 完全自主；
- λ 温和化（能力分主导、成本微调封顶 0.5、同分 tie-break）+ paretoEnabled 默认开；
- 延迟冷启动代理（THINKING_LATENCY_PROXY，档位越高假设越慢，遥测回填后切真实值）；
- rank 归一化升级百分制（第一名 100、最末名 1，档案仍存绝对分）。

峰谷计价粒度说明：DeepSeek 官方计费按北京时间整点窗口（9-12、14-18，工作日）计费，
time_factor 的整点分桶与官方账单口径一致（非近似）；若接入秒级计价厂商再行插值。
"""
import datetime
import json
import math
import os
import random
import threading
import time
from pathlib import Path

try:
    from . import params as params_mod
    from . import token_profiles
except ImportError:  # 直接执行/老导入路径
    import params as params_mod  # type: ignore
    import token_profiles  # type: ignore

BASE = Path(__file__).resolve().parent.parent  # council/

# ---------------- 思考档位序（P0-4 档位→thinking 硬约束） ----------------

# 每个模型家族的 thinking 档位按消耗升序排列（v15.4：档位上限约束已删除，此表仅用于
# 延迟冷启动代理的档位归属判断与 thinking_not_allowed 档位合法性护栏）。
THINKING_ORDER = {
    "deepseek": ["off", "low", "high", "max"],
    "MiniMax": ["off", "minimal", "low", "medium", "high"],
}

# v15.4：延迟冷启动代理（P50 毫秒，保守假设——档位越高思考越久）。
# runtime.latencyP50Ms 遥测回填后自动切换真实值；代理值只影响延迟惩罚项与 tie-break。
THINKING_LATENCY_PROXY = {
    "off": 2000, "minimal": 3000, "low": 5000, "medium": 10000, "high": 20000, "max": 30000,
}

def thinking_rank(base_model: str, thinking: str) -> int:
    """返回 thinking 档位在本家族的消耗序位（0 起）；未知档位返回家族长度（视为超限）。"""
    for prefix, order in THINKING_ORDER.items():
        if base_model.startswith(prefix):
            return order.index(thinking) if thinking in order else len(order)
    return 0  # 未知家族不约束

# ---------------- Elo 横向比较（P1-3） ----------------

_ELO_FILE = BASE / "elo.json"
_elo_cache = {"mtime": 0.0, "data": {}}

def load_elo() -> dict:
    """读取 pairwise.py 产出的 elo.json（ratings: {baseModel: elo}）。失败静默回退空表。"""
    if not _ELO_FILE.exists():
        return {}
    try:
        mtime = _ELO_FILE.stat().st_mtime
        if _elo_cache["mtime"] != mtime:
            _elo_cache["data"] = json.loads(
                _ELO_FILE.read_text(encoding="utf-8")).get("ratings", {})
            _elo_cache["mtime"] = mtime
        return _elo_cache["data"]
    except Exception:
        return {}

# ---------------- 数据加载 ----------------

def load_capabilities() -> dict:
    p = BASE / "capabilities.json"
    if not p.exists():
        return {"schemaVersion": 2, "revision": 0, "models": {}, "dimensions": []}
    return json.loads(p.read_text(encoding="utf-8"))

def load_pricing() -> dict:
    p = BASE / "pricing-profiles.json"
    if not p.exists():
        return {"providers": {}, "exchangeRates": {}}
    return json.loads(p.read_text(encoding="utf-8"))

def load_balance_snapshot() -> dict:
    p = BASE / "balance-snapshot.json"
    if p.exists():
        try:
            return json.loads(p.read_text(encoding="utf-8"))
        except Exception:
            pass
    return {}

def load_fx_rate() -> dict:
    """exchange-rates.json → {usdToCny, ...}；缺失时回退 pricing 的静态汇率。"""
    p = BASE / "exchange-rates.json"
    if p.exists():
        try:
            return json.loads(p.read_text(encoding="utf-8"))
        except Exception:
            pass
    try:
        return {"usdToCny": load_pricing().get("exchangeRates", {}).get("USD"), "source": "pricing-fallback"}
    except Exception:
        return {}

def fx_status() -> dict:
    """P2-2：汇率陈旧等级。level: 0=正常 / 1=黄灯(落后≥staleWarnDays 个交易日) /
    2=停机(落后≥staleHaltDays 个交易日，应暂停 CNY 记账)。
    交易日落后天数：从 publishDate 数到「当前应已发布的最新交易日」（工作日语义）。"""
    rates = load_fx_rate()
    fx = params_mod.load().get("fx", params_mod.DEFAULTS["fx"])
    warn_days = int(fx.get("staleWarnDays", 1))
    halt_days = int(fx.get("staleHaltDays", 3))
    out = {"usdToCny": rates.get("usdToCny"), "stale": bool(rates.get("stale")),
           "staleReasons": list(rates.get("staleReasons") or []),
           "level": 0, "tradingDaysBehind": 0, "source": rates.get("source")}
    if not rates.get("stale") or rates.get("usdToCny") is None:
        return out
    publish = rates.get("publishDate")
    days_behind = 0
    if publish:
        try:
            from .fetch_exchange_rate import _latest_expected_date
            pd = datetime.datetime.strptime(str(publish)[:10], "%Y-%m-%d").date()
            now = datetime.datetime.now(datetime.timezone(datetime.timedelta(hours=8)))
            expected = _latest_expected_date(now)
            d = pd
            while d < expected:
                d += datetime.timedelta(days=1)
                if d.weekday() < 5:
                    days_behind += 1
        except Exception:
            days_behind = warn_days  # 日期解析失败 → 保守按黄灯
    out["tradingDaysBehind"] = days_behind
    if days_behind >= halt_days:
        out["level"] = 2
    elif days_behind >= warn_days:
        out["level"] = 1
    return out

# ---------------- token 预估器 ----------------

CHAR_PER_TOKEN = {"cjk": 0.66, "latin": 4.0, "code": 3.5}  # 每 token 字符数（估算）

def estimate_tokens(text: str) -> int:
    cjk = sum(1 for ch in text if '\u4e00' <= ch <= '\u9fff')
    latin = sum(1 for ch in text if ch.isascii() and ch.isalpha())
    digits_other = sum(1 for ch in text if not ('\u4e00' <= ch <= '\u9fff') and not (ch.isascii() and ch.isalpha()))
    return int(cjk * 1.5 + (latin + digits_other) / 4) + 8  # +8 系统提示开销

# ---------------- 时间因子（峰谷价） ----------------

def time_factor(provider: str, now: datetime.datetime = None) -> float:
    pricing = load_pricing().get("providers", {}).get(provider, {})
    tf = pricing.get("timeFactor")
    if not tf:
        return 1.0
    now = now or datetime.datetime.now()
    if tf.get("workdaysOnly", True) and now.weekday() >= 5:
        return tf["offpeak"]
    hm = now.strftime("%H:%M")
    for start, end in tf["peakHours"]:
        if start <= hm < end:
            return tf["peak"]
    return tf["offpeak"]

# ---------------- 额度因子（scarcity，多窗口取最紧） ----------------

def norm_percent(pct) -> float:
    """百分比语义守卫（H3 单位契约）：快照存整数百分比（96=96%）。
    若上游误传小数比例（0.96），自动 ×100 修复；越界收敛到 [0,100]。"""
    if pct is None:
        return 100.0
    pct = float(pct)
    if 0.0 < pct <= 1.0:
        pct *= 100.0  # 小数比例语义 → 归一为百分比
    return max(0.0, min(100.0, pct))

def quota_factor(provider: str, balance: dict) -> float:
    """余额/额度稀缺因子。1.0=充裕；耗尽=inf（应被护栏硬剔除）。"""
    pricing = load_pricing().get("providers", {}).get(provider, {})
    ptype = pricing.get("type")
    if ptype == "free_pool":
        windows = pricing.get("quotaWindows", [])
        worst = 1.0
        for w in windows:
            key = f"{provider}:{w}"
            pct = balance.get(key)
            if pct is None:
                worst = max(worst, 2.0)  # 查不到 → 保守按吃紧
                continue
            frac = norm_percent(pct) / 100.0  # 整数百分比 → 比例
            if frac <= 0.01:
                return float("inf")
            if frac <= 0.2:
                worst = max(worst, 3.0 - (frac / 0.2) * 1.0)  # 20%→2.0, 0→3.0
            elif frac <= 0.5:
                worst = max(worst, 1.0 + (0.5 - frac) / 0.3)  # 50%→1.0, 20%→2.0
        return worst
    # pay_per_token：余额快照
    bal = balance.get(f"{provider}:balance")
    if bal is None:
        return 2.0  # 查不到 → 保守
    # 与「月消耗速率」比较：用快照里附带的估算
    monthly = balance.get(f"{provider}:monthly_estimate")
    if monthly and monthly > 0:
        months = bal / monthly
        if months <= 0.1:
            return float("inf")
        if months <= 0.5:
            return 3.0 - (months - 0.1) / 0.4 * 1.0
        if months <= 1.5:
            return 1.0 + (1.5 - months)
        return 1.0
    return 1.0

# ---------------- 成本函数（表驱动 thinking 系数 + 缓存命中 + 币种换算） ----------------

DEFAULT_THINKING_MULT = {"off": 1.0, "minimal": 1.05, "low": 1.1, "medium": 1.2, "high": 1.4, "max": 1.5}

def thinking_multiplier(provider: str, model: str, thinking: str) -> float:
    """thinking 系数：优先 pricing-profiles.json 的 per-provider/per-model 表（H3 校准入口），
    缺失回退保守默认值。"""
    pricing = load_pricing().get("providers", {}).get(provider, {})
    table = pricing.get("thinkingMult")
    if not isinstance(table, dict):
        table = (pricing.get("models") or {}).get(model, {}).get("thinkingMult")
    if isinstance(table, dict):
        return float(table.get(thinking, table.get("default", DEFAULT_THINKING_MULT.get(thinking, 1.2))))
    return DEFAULT_THINKING_MULT.get(thinking, 1.2)

def effective_cost_cny(provider: str, model: str, thinking: str,
                       est_input_tokens: int, est_output_tokens: int,
                       now=None, balance=None, cache_hit_rate: float = 0.0,
                       calib_role: str = None) -> dict:
    """effectiveCost = base × thinkingMult × quotaFactor（规划口径，供选择/预算）。
    baseCostCny = ((in×(1-chr)+cache×chr)×in价 + out×out价)/1e6（对账口径，P0-3：
    与 _cost_pair 的 actual 同构——actual 只乘真实单价不乘规划系数，drift 才可比）。
    v15.3：base 乘 per-(model[, role]) 校准系数（calib_role=None 时用模型级平均，
    _cost_pair 传具体 role 用 role 级），修正估算系统性偏差闭环。
    免费池/单价缺失 → cost=None，由 scarcity 单独承担成本项。
    返回 CostContext 全字段（单位后缀，杜绝裸值歧义）。"""
    from .cost_context import CostContext
    pricing = load_pricing().get("providers", {}).get(provider, {})
    balance = balance or load_balance_snapshot()
    fx = load_fx_rate().get("usdToCny")
    tf = time_factor(provider, now)
    qf = quota_factor(provider, balance)
    mp = (pricing.get("models") or {}).get(model) or {}
    in_price = mp.get("inputCnyPerMTok")
    out_price = mp.get("outputCnyPerMTok")
    cache_price = mp.get("cacheInputCnyPerMTok")
    think_mult = thinking_multiplier(provider, model, thinking)
    chr_ = max(0.0, min(0.9, float(cache_hit_rate or 0.0)))
    cost = None
    base_cost = None
    if in_price is not None and out_price is not None:
        rate_in = (in_price.get("peak") if tf > 0.9 else in_price.get("offpeak")) or 0
        rate_out = (out_price.get("peak") if tf > 0.9 else out_price.get("offpeak")) or 0
        rate_cache = (cache_price or {}).get("peak" if tf > 0.9 else "offpeak") if cache_price else None
        if rate_cache is None:
            rate_cache = rate_in
        effective_in = est_input_tokens * ((1 - chr_) * rate_in + chr_ * rate_cache)
        base_cost = (effective_in + est_output_tokens * rate_out) / 1_000_000
        # v15.3（P0-3 校准闭环）：乘校准系数（cost_calibrate 每日回填），
        # 修正估算系统性偏差（此前 drift -15.87% 无收敛机制，永远漂）
        base_cost = base_cost * token_profiles.calib_for(model, calib_role)
        cost = base_cost * think_mult * qf
    elif qf == float("inf"):
        cost = float("inf")
    ctx = CostContext(
        provider=provider, model=model, thinking=thinking,
        inputCnyPerMTok=in_price, outputCnyPerMTok=out_price,
        cacheInputCnyPerMTok=cache_price, timeFactor=tf, quotaFactor=qf,
        thinkingMult=think_mult, estInputTokens=est_input_tokens,
        estOutputTokens=est_output_tokens, cacheHitRate=chr_,
        costCny=cost, baseCostCny=base_cost, usdToCny=fx)
    return ctx.as_dict()

# ---------------- 评分函数（缺项回退 + 置信加权 + tie-break） ----------------
# 参数外置（评审报告 M 项）：默认值=拍板基线，council-params.json 可覆盖（selection.*）

def _sel_params():
    return params_mod.load().get("selection", params_mod.DEFAULTS["selection"])

def _div_bonus():
    return float(_sel_params().get("divBonus", 0.3))

def _sample_knee():
    return int(_sel_params().get("sampleKnee", 20))

DIV_BONUS = 0.3          # 兼容旧引用：实际取值走 _div_bonus()
SAMPLE_KNEE = 20         # 兼容旧引用：实际取值走 _sample_knee()

# ---------------- v15.3/v15.4：能力分 rank 归一化（同质化治理 + 百分制） ----------------

def build_rank_table(caps_models: dict) -> dict:
    """per-dim 的 rank 归一化表：{dim: {cid: norm_score(1-100)}}。

    元评审实证（2026-08-24）：16 条目 9 维能力分集中在 9.5-10，Σ权重×能力分项近似常数，
    selector 退化（区分度≈0）。rank 归一化把每维分数映射到排序位置：
    **第一名 100 分、最末名 1 分**（v15.4 the maintainer decided，百分制线性展开）。
    同分取平均排名（并列不虚排）；dim 内仅 1 个有值 → 无法归一化，回退中位 50.5。
    档案仍存绝对分（0-10，EMA 融合在绝对空间），本表是选择/展示层的派生数据。"""
    dims = set()
    for cid, cand in caps_models.items():
        for d in (cand.get("capabilities") or {}):
            dims.add(d)
    table = {}
    for d in sorted(dims):
        scored = []
        for cid, cand in caps_models.items():
            e = (cand.get("capabilities") or {}).get(d) or {}
            v = e.get("score")
            if isinstance(v, (int, float)):
                scored.append((cid, float(v)))
        scored.sort(key=lambda x: x[1])
        n = len(scored)
        ranks = {}
        if n <= 1:
            for cid, _v in scored:
                ranks[cid] = 50.5
            table[d] = ranks
            continue
        i = 0
        while i < n:
            j = i
            while j + 1 < n and scored[j + 1][1] == scored[i][1]:
                j += 1
            avg_rank = (i + j) / 2.0          # 并列取平均排名
            norm = round(1 + avg_rank / (n - 1) * 99, 2)  # v15.4：第一名 100，最末名 1
            for k in range(i, j + 1):
                ranks[scored[k][0]] = norm
            i = j + 1
        table[d] = ranks
    return table

def score_candidate(cand: dict, task_vector: dict, ctx: dict,
                    rank_table: dict = None, cid: str = None):
    """返回 (score, meta)。meta: {fallback, fallback_dims, confidence}。
    L2：缺项回退模型自身历史均值并标记 fallback=true；样本不足按 shrinkage 收缩向均值。
    v15.3：rank_table 提供时，per-dim 原始分替换为 rank 归一化分（0-10 均匀展开，
    恢复同质化档案的区分度）；fallback/收缩全部发生在归一化空间，杜绝混用尺度。"""
    caps = cand.get("capabilities", {})
    own = [e.get("score") for e in caps.values()
           if isinstance(e, dict) and e.get("score") is not None]
    own_mean = (sum(own) / len(own)) if own else None
    if rank_table is not None and cid:
        # 归一化空间：own_mean 改为该候选各维归一化分均值
        own_norm = [rank_table[d].get(cid) for d in rank_table
                    if rank_table[d].get(cid) is not None]
        if own_norm:
            own_mean = sum(own_norm) / len(own_norm)
    s = 0.0
    fallback_dims = []
    conf_total = 0.0
    used = 0
    knee = _sample_knee()
    for d, w in task_vector.items():
        entry = caps.get(d) or {}
        v = entry.get("score")
        n = entry.get("samples") or entry.get("runtimeSamples") or 0
        if rank_table is not None and cid:
            v_norm = (rank_table.get(d) or {}).get(cid)
            if v is not None:
                if v_norm is not None:
                    v = v_norm
                # v_norm 为 None（该维无分但有原始分，罕见）→ 保留原始分
            elif v_norm is not None:
                v = v_norm  # 原始缺失但 rank 表有 → 用归一化分（不再走 own_mean 回退）
                n = 0
        if v is None:
            v = own_mean
            n = 0
            if v is not None:
                fallback_dims.append(d)
        if v is None:  # 完全无数据 → 该维 0 贡献并标记 fallback
            fallback_dims.append(d)
            continue
        wc = min(1.0, (n or 0) / knee)  # 置信加权
        if n < knee and own_mean is not None:
            v = v * wc + own_mean * (1 - wc)  # shrinkage：样本少收缩向模型均值
        s += w * v
        used += 1
        conf_total += wc
    meta = {"fallback": bool(fallback_dims), "fallback_dims": fallback_dims,
            "confidence": round(conf_total / used, 3) if used else 0.0,
            "ownMean": round(own_mean, 2) if own_mean is not None else None,
            "rankNormalized": rank_table is not None and bool(cid)}
    # 成本项（v15.4 温和化：能力分主导、成本只做微调、同分 tie-break——
    # 封顶从 3.0 降为 0.5，余额耗尽仍扣 0.5 而非 5.0；"同样成本不换更差结果、
    # 更高成本换更好结果可接受"，不限制模型发挥）
    cost = ctx.get("cost_cny")
    if cost is not None and cost != float("inf"):
        s -= ctx["lambda_"] * min(0.5, math.log1p(max(cost, 0) * 1000))
    elif cost == float("inf"):
        s -= ctx["lambda_"] * 0.5
    # 延迟项（P0-4：数据源扩展——caps.runtime.latencyP50Ms 由运行期回填；
    # v15.4 冷启动代理：无遥测数据时按档位给保守代理值，防止 max 档被误判为与 off 一样快）
    lat = cand.get("latencyP50Ms")
    if lat is None:
        rt = cand.get("runtime") or {}
        lat = rt.get("latencyP50Ms")
    if lat is None:
        lat = THINKING_LATENCY_PROXY.get(cand.get("thinking"), 5000)
    if lat is not None and ctx.get("mu", 0) > 0:
        s -= ctx["mu"] * min(2.0, lat / 120_000)
    # P1-3：Elo 横向比较软惩罚（低 Elo 降权，不剔除）
    if _sel_params().get("eloEnabled", True):
        elo = load_elo()
        if elo:
            er = elo.get(cand.get("baseModel"))
            vals = [v for v in elo.values() if isinstance(v, (int, float))]
            if er is not None and vals:
                mean = sum(vals) / len(vals)
                s -= float(_sel_params().get("eloPenaltyScale", 0.2)) * max(0.0, (mean - er) / 100.0)
    # 多样性加分
    used_providers = ctx.get("used_providers", set())
    if cand.get("provider") and cand["provider"] not in used_providers:
        s += _div_bonus()
    return round(s, 4), meta

# ---------------- 护栏过滤（优先级明确 + 事件日志） ----------------

GUARD_PRIORITY = [
    "identity_unknown",
    "unstable_member_excluded_from_pool",
    "thinking_not_allowed",
    "circuit_open",
    "balance_exhausted",
    "self_verify_ban",
]

GUARD_EVENTS = Path(os.environ.get("COUNCIL_GUARD_EVENTS", str(BASE / "guardrail-events.jsonl")))

# v15.3（元评审 P0）：pool_excluded 同 run 去重节流。预过滤本身每 select 调用都执行，# 但同一 run 内排除集合不变（banned/allowlist/档案版本稳定）时不再重复写事件，
# 只在该 run 的排除集合首次出现或变化时记一条。20-18-15 run 实测 10 条/run 降为 ~2 条/run。
_POOL_EVENTS_SEEN = {}   # run_id -> 排除集合签名
_POOL_EVENTS_MAX = 128   # 防内存增长：超限整体清空（低频事件，误伤仅为多记一条）

def _semantic_split(excluded: dict) -> dict:
    """v15.5b（元评审 F3）：pool_excluded 分流「质量型 / 资源型 / 调度型」——
    质量型=候选自身不合格（身份/自验/稳定性/档位）；资源型=外部资源约束（余额/熔断）。"""
    quality = {"self_verify_ban", "identity_unknown", "unstable_member_excluded_from_pool",
               "thinking_not_allowed"}
    resource = {"balance_exhausted", "circuit_open", "circuit_half_open_probing"}
    out = {"quality": {}, "resource": {}, "schedule": {}}
    for k, v in excluded.items():
        if k in quality:
            out["quality"][k] = len(v)
        elif k in resource:
            out["resource"][k] = len(v)
        else:
            out["schedule"][k] = len(v)
    return out


def _pool_excluded_signature(excluded: dict, caps_revision, banned: set) -> str:
    return json.dumps({"excluded": {k: sorted(v) for k, v in excluded.items()},
                       "revision": caps_revision, "banned": sorted(banned)},
                      sort_keys=True, ensure_ascii=False)

def _log_guard_event(cid: str, provider: str, reason_code: str,
                     run_id: str = None, threshold=None, measured=None,
                     caps_revision: int = None):
    """H4：每次护栏剔除写 guardrail-events.jsonl——run_id、护栏名、阈值、实测值、时间
    五要素齐全（评审报告 H 项：事后可复盘「哪道护栏/什么阈值/什么实测值/什么时刻触发」）。
    v15.2：事件附带 capabilitiesRevision（本次选择加载的档案版本，P0-2 可追溯）。"""
    try:
        with GUARD_EVENTS.open("a", encoding="utf-8") as f:
            f.write(json.dumps({
                "ts": datetime.datetime.now().astimezone().isoformat(),
                "run_id": run_id,
                "guard_name": reason_code,
                "candidate": cid,
                "model@thinking": cid,
                "provider": provider,
                "reason_code": reason_code,
                "threshold": threshold,
                "measured": measured,
                "capabilitiesRevision": caps_revision,
                "snapshot_ref": int(time.time()),
            }, ensure_ascii=False, default=str) + "\n")
    except OSError:
        pass  # 事件日志失败不阻断选择

def passes_guards(cand: dict, ctx: dict) -> tuple:
    """返回 (ok, reason)。冲突时按 GUARD_PRIORITY 取最高优先级 reason_code。
    每个 reason 附带 (threshold, measured) 用于结构化事件。"""
    reasons = {}  # code -> (threshold, measured)
    # 1 可用性：stable=False 一律剔除（旧逻辑非 strict 模式放行 "ok"，导致 unstable 模型参选）
    if not cand.get("stable", True):
        reasons["unstable_member_excluded_from_pool"] = (
            {"stable": "required=true"},
            {"stable": bool(cand.get("stable", True))})
    # 身份未知（无可靠 API 路由/来源不明的模型）一律剔除
    if cand.get("identityUnknown"):
        reasons["identity_unknown"] = (
            {"identityUnknown": "required=true"},
            {"identityUnknown": True})
    allowed_t = _allowed_thinking(cand["baseModel"])
    if cand.get("thinking") not in allowed_t:
        reasons["thinking_not_allowed"] = (
            {"allowedThinking": allowed_t},
            {"thinking": cand.get("thinking")})
    # 熔断（半开探测：仅放行一个探测候选）
    cstate = _circuit_state(cand["baseModel"])
    if cstate == "open":
        reasons["circuit_open"] = (
            {"circuitState": "closed"},
            {"circuitState": cstate, "baseModel": cand["baseModel"]})
    elif cstate == "half_open":
        if not _probe_begin(cand["baseModel"]):
            reasons["circuit_half_open_probing"] = (
                {"probeConcurrency": 1},
                {"probing": True, "baseModel": cand["baseModel"]})
    # 3 余额红线
    if ctx.get("cost_cny") == float("inf"):
        reasons["balance_exhausted"] = (
            {"quotaFactor": "<inf", "cost_cny": "finite"},
            {"quotaFactor": ctx.get("quota_factor"),
             "cost_cny": "inf",
             "balanceSnapshot": {
                 k: v for k, v in (ctx.get("balance") or {}).items()
                 if isinstance(v, (int, float))}})
    # 2 自验禁令（调用方在 ctx 传 banned_models）
    if cand["baseModel"] in ctx.get("banned_models", set()):
        reasons["self_verify_ban"] = (
            {"bannedModels": sorted(ctx.get("banned_models", set()))},
            {"baseModel": cand["baseModel"]})
    if not reasons:
        return True, "ok"
    run_id = ctx.get("run_id")
    caps_revision = ctx.get("caps_revision")
    for code in GUARD_PRIORITY:
        if code in reasons:
            threshold, measured = reasons[code]
            _log_guard_event(cand.get("candidateId") or f"{cand['baseModel']}__{cand.get('thinking', 'off')}",
                             cand.get("provider"), code,
                             run_id=run_id, threshold=threshold, measured=measured,
                             caps_revision=caps_revision)
            return False, code
    code = sorted(reasons)[0]
    threshold, measured = reasons[code]
    _log_guard_event(cand.get("candidateId") or f"{cand['baseModel']}__{cand.get('thinking', 'off')}",
                     cand.get("provider"), code,
                     run_id=run_id, threshold=threshold, measured=measured,
                     caps_revision=caps_revision)
    return False, code

def _allowed_thinking(base_model: str) -> list:
    if base_model.startswith("MiniMax"):
        return ["off", "minimal", "low", "medium", "high"]
    return ["off", "low", "high", "max"]

# ---------------- 熔断器（三态 + 指数退避 + 半开探测） ----------------

_CIRCUIT_FILE = Path(os.environ.get("COUNCIL_CIRCUIT_FILE", str(BASE / "circuit-state.json")))

# 参数外置（评审报告 M 项）：默认值=拍板基线，council-params.json 的 circuit.* 可覆盖。
CIRCUIT_FAILURE_THRESHOLD = 3   # 窗口内连续失败数触发熔断
CIRCUIT_FAILURE_WINDOW_S = 600  # 10 分钟无新失败 → 计数清零（瞬时抖动不熔断）
CIRCUIT_BASE_BACKOFF_S = 300    # 首次熔断冷却 5 分钟
CIRCUIT_MAX_BACKOFF_S = 3600    # 指数退避上限 1 小时
CIRCUIT_PROBE_TTL_S = 300       # 半开探测 5 分钟无结果视为放弃

def _circuit_params() -> dict:
    return params_mod.load().get("circuit", params_mod.DEFAULTS["circuit"])

def _load_circuit() -> dict:
    if _CIRCUIT_FILE.exists():
        try:
            return json.loads(_CIRCUIT_FILE.read_text(encoding="utf-8"))
        except Exception:
            pass
    return {}

# v15.3：circuit-state.json 并发写加固——exec/verifier 多线程同时结算熔断器，
# 且多个 council 进程（judge_drift/run_council 并行）可能同时写，tmp+replace 会撞
# WinError 32（2026-08-24 实测炸掉整个 run）。线程锁 + 指数退避重试双保险。
_circuit_lock = threading.Lock()

def _write_circuit(st: dict):
    tmp = _CIRCUIT_FILE.with_suffix(".json.tmp")
    with _circuit_lock:
        for attempt in range(6):
            try:
                tmp.write_text(json.dumps(st, ensure_ascii=False, indent=2), encoding="utf-8")
                tmp.replace(_CIRCUIT_FILE)
                return
            except OSError as e:
                if attempt == 5:
                    # 熔断状态是旁路防护数据：写失败不炸 run，但显式告警到 stderr 保留可见性
                    import sys as _sys
                    print(f"[council] circuit-state 写入失败（放弃本次）：{e}", file=_sys.stderr)
                    return
                time.sleep(0.05 * (attempt + 1))

def _circuit_state(model: str) -> str:
    """closed / open / half_open（冷却到期自动进入半开）。"""
    st = _load_circuit().get(model, {})
    state = st.get("state", "closed")
    if state == "open" and st.get("open_until", 0) < time.time():
        return "half_open"
    return state

def _probe_begin(model: str) -> bool:
    """半开探测：同一模型同时只允许一个探测候选。探测超时未结算则自动释放。"""
    st = _load_circuit()
    entry = st.get(model, {"state": "closed", "failures": 0})
    probing = entry.get("probing", False)
    probe_ts = entry.get("probe_ts", 0)
    ttl = int(_circuit_params().get("probeTtlS", CIRCUIT_PROBE_TTL_S))
    if probing and time.time() - probe_ts < ttl:
        return False
    entry["probing"] = True
    entry["probe_ts"] = time.time()
    st[model] = entry
    _write_circuit(st)
    return True

def record_failure(model: str):
    """失败结算：半开探测失败 → 立即回到 open 并加倍退避；closed 下窗口内累计 N 次熔断。
    open 已过冷却期（时间上等效 half_open）的探测失败同样按半开处理。"""
    cp = _circuit_params()
    threshold = int(cp.get("failureThreshold", CIRCUIT_FAILURE_THRESHOLD))
    window_s = int(cp.get("failureWindowS", CIRCUIT_FAILURE_WINDOW_S))
    base_s = int(cp.get("baseBackoffS", CIRCUIT_BASE_BACKOFF_S))
    max_s = int(cp.get("maxBackoffS", CIRCUIT_MAX_BACKOFF_S))
    st = _load_circuit()
    entry = st.get(model, {"state": "closed", "failures": 0, "trips": 0})
    last = entry.get("last_failure", 0)
    now = time.time()
    state = entry.get("state", "closed")
    effective = "half_open" if (state == "open" and entry.get("open_until", 0) < now) else state
    if effective == "half_open" or (now - last) > window_s:
        entry["failures"] = 0  # 半开探测失败或窗口过期 → 重新计数
    entry["failures"] = entry.get("failures", 0) + 1
    entry["last_failure"] = now
    if effective == "half_open" or entry["failures"] >= threshold:
        trips = entry.get("trips", 0) + 1
        backoff = min(base_s * (2 ** (trips - 1)), max_s)
        entry.update({"state": "open", "trips": trips, "open_until": now + backoff,
                      "probing": False})
    st[model] = entry
    _write_circuit(st)

def record_success(model: str):
    st = _load_circuit()
    entry = st.get(model, {"state": "closed", "failures": 0, "trips": 0})
    entry.update({"state": "closed", "failures": 0, "probing": False, "trips": 0})
    st[model] = entry
    _write_circuit(st)

# ---------------- ε-greedy 探索 ----------------

def epsilon_greedy(ranked: list, ctx: dict) -> list:
    """冷启动探索：样本少的模型获得探索概率。"""
    eps = ctx.get("epsilon", 0.0)
    if eps <= 0 or random.random() >= eps:
        return ranked
    # 在非榜首中随机挑一个换到第二位置（保持第一名稳定）
    if len(ranked) >= 3:
        idx = random.randrange(1, len(ranked))
        ranked[1], ranked[idx] = ranked[idx], ranked[1]
    return ranked

# ---------------- 主入口：选择 ----------------

def _rank_key(r):
    """tie-break 契约（L1）：分数相等时按 cost 升序 → latency 升序 → cid 字典序。
    v15.4：latency 缺失时用档位代理（见 THINKING_LATENCY_PROXY）。"""
    cid, cand, s, cost_info = r
    cost = (cost_info or {}).get("cost_cny")
    cost_key = float(cost) if isinstance(cost, (int, float)) and cost != float("inf") else 0.0
    lat = cand.get("latencyP50Ms") or (cand.get("runtime") or {}).get("latencyP50Ms")
    if lat is None:
        lat = THINKING_LATENCY_PROXY.get(cand.get("thinking"), 5000)
    lat_key = float(lat) if lat is not None else float("inf")
    return (-s, cost_key, lat_key, cid)

def _cost_of(r):
    """候选成本（排序/前沿用）：inf 视为 0 成本键（免费池）。"""
    ci = r[3] if len(r) > 3 else None
    cost = (ci or {}).get("cost_cny")
    return float(cost) if isinstance(cost, (int, float)) and cost != float("inf") else 0.0

def _apply_pareto(valid: list) -> list:
    """P2-1：在 (score, cost) Pareto 前沿上的候选加 paretoBonus 软加分（不剔除任何候选）。"""
    bonus = float(_sel_params().get("paretoBonus", 0.1))
    by_cost = sorted(valid, key=lambda r: (_cost_of(r), -r[2]))
    frontier = set()
    best = float("-inf")
    for cid, cand, s, ci in by_cost:
        if s > best:
            frontier.add(cid)
            best = s
    out = []
    for r in valid:
        if r[0] in frontier:
            cid, cand, s, ci = r
            out.append((cid, cand, round(s + bonus, 4), ci))
        else:
            out.append(r)
    out.sort(key=_rank_key)
    return out

def select(task_vector: dict, ctx: dict, caps_data: dict = None) -> list:
    """为子任务选出模型候选（排序后列表）。
    ctx 需要：lambda_, mu, used_providers, banned_models, balance, epsilon, est_tokens；
    可选：caps_revision、cache_hit_rate。
    v15.2：静态不合格候选（identityUnknown/stable=false/allowlist 外/自验禁令）
    在评分前预过滤，每个调用只记一条 pool_excluded 汇总事件（P0-2 护栏降噪）。
    v15.4：删除 max_thinking_rank 档位上限——每个档位视作不同模型，完全由 selector 自主
    （延迟档位代理保证无遥测数据时高思考档位不吃延迟评估的红利）。"""
    caps_data = caps_data or load_capabilities()
    pricing = load_pricing()
    selp = _sel_params()
    caps_revision = caps_data.get("revision")
    allowlist = set(selp.get("allowlist") or [])
    pre_filter = bool(selp.get("staticEligibilityPreFilter", True))
    banned = set(ctx.get("banned_models") or set())
    excluded = {}  # reason -> [cids]
    ranked = []
    # v15.3：能力分 rank 归一化（selection.rankNormalize，默认开）——
    # 元评审实证分数同质化（9.5-10）导致 Σ权重×能力分项近似常数，selector 退化
    rank_table = build_rank_table(caps_data.get("models", {})) \
        if selp.get("rankNormalize", True) else None
    for cid, cand in caps_data.get("models", {}).items():
        provider = cand.get("provider")
        base = cand.get("baseModel", cid)
        if pre_filter:
            reason = None
            if allowlist and base not in allowlist:
                reason = "allowlist_excluded"
            elif cand.get("identityUnknown"):
                reason = "identity_unknown"
            elif not cand.get("stable", True):
                reason = "unstable_member_excluded_from_pool"
            elif base in banned:
                reason = "self_verify_ban"
            if reason:
                excluded.setdefault(reason, []).append(cid)
                continue
        ptype = pricing.get("providers", {}).get(provider, {}).get("type", "")
        est_in = ctx.get("est_input_tokens", 800)
        est_out = ctx.get("est_output_tokens", 400)
        cost_info = effective_cost_cny(provider, cand["baseModel"], cand.get("thinking"),
                                       est_in, est_out, now=ctx.get("now"),
                                       balance=ctx.get("balance"),
                                       cache_hit_rate=ctx.get("cache_hit_rate", 0.0))
        cand_ctx = {**ctx, "cost_cny": cost_info["cost_cny"],
                    "quota_factor": cost_info["quota_factor"],
                    "run_id": ctx.get("run_id"),
                    "caps_revision": caps_revision}
        ok, reason = passes_guards(cand, cand_ctx)
        if not ok and reason not in ("ok",):
            ranked.append((cid, cand, None, reason))
            continue
        s, meta = score_candidate(cand, task_vector, cand_ctx,
                                   rank_table=rank_table, cid=cid)
        cost_info["scoreMeta"] = meta
        ranked.append((cid, cand, s, cost_info))
    if excluded:
        # P0-2：预过滤只记一条汇总事件（含各原因计数与档案 revision），替代逐候选刷屏；
        # v15.3：同 run 内排除集合不变时去重（见 _pool_excluded_signature）。
        sig = _pool_excluded_signature(excluded, caps_revision, banned)
        run_id = ctx.get("run_id")
        if len(_POOL_EVENTS_SEEN) >= _POOL_EVENTS_MAX:
            _POOL_EVENTS_SEEN.clear()
        if _POOL_EVENTS_SEEN.get(run_id) != sig:
            _POOL_EVENTS_SEEN[run_id] = sig
            _log_guard_event("pool", "", "pool_excluded",
                             run_id=run_id,
                             threshold={"preFilter": True, "allowlist": sorted(allowlist) or None},
                             measured={"excludedByReason": {k: len(v) for k, v in excluded.items()},
                                       "excludedCids": {k: sorted(v) for k, v in excluded.items()},
                                       "semantic": _semantic_split(excluded),
                                       "totalCandidates": len(caps_data.get("models", {})),
                                       "bannedModels": sorted(banned)},
                             caps_revision=caps_revision)
    valid = [r for r in ranked if r[2] is not None]
    valid.sort(key=_rank_key)
    if selp.get("paretoEnabled"):
        valid = _apply_pareto(valid)
    valid = epsilon_greedy(valid, ctx)
    return valid
