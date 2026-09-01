"""v15.1 参数外置：所有选择权重/熔断/收敛/超时参数从 council-params.json 加载。

动机（2026-08-24 council 评审报告 M 项）：选择权重、熔断参数原先硬编码在模块常量里，
调优必须改代码。现在统一收敛到 ~< user-data-dir >/council-params.json：
- 文件缺失/字段缺失 → 用 DEFAULTS 兜底（行为与旧硬编码一致，向后兼容）；
- 测试可用环境变量 COUNCIL_PARAMS_FILE 指向临时文件隔离；
- 改动走文件评审（diff 可见），无需重装任何组件。

用法：
  from orchestrator import params
  p = params.load()                       # 合并后的全量 dict
  params.get(p, "circuit.failureThreshold")  # 点路径取值
"""
import json
import os
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent  # council/
PARAMS_FILE = Path(os.environ.get("COUNCIL_PARAMS_FILE", str(BASE / "council-params.json")))

# 默认值 = v15 硬编码基线（评审报告前已拍板的参数，见 DESIGN-v14.md §4/§5.2/§6/§12）
DEFAULTS = {
    "schemaVersion": 1,
    # 档位参数（v15.4：budgetCny/costGuardMul/maxThinkingRank 已删——预算不做用量控制、
    # 档位上限交由 selector 自主；档位本质 = 复杂度刻度 θ/maxIter/maxSubtasks）
    # wallBudgetS：v15.4 统一 1800s 三档同值——技术安全线（防模型异常），非质量预算；
    #   宿主 runPy 超时 1920s（+120s 收尾余量），单次调用 total 900s。
    # λ：v15.4 温和化——能力分主导、成本微调（惩罚封顶 0.5），deep 档 λ≈0。
    # maxTaskTokens：v15.3 保留（fast 任务长度护栏——速度技术护栏性质）。
    "tiers": {
        "fast": {"theta": 7.0, "lambda_": 0.15, "maxIter": 3,
                 "tieBreak": "cost_then_latency",
                 "wallBudgetS": 1800, "maxSubtasks": 3, "maxTaskTokens": 4000},
        "standard": {"theta": 8.0, "lambda_": 0.1, "maxIter": 5,
                     "tieBreak": "cost_then_latency",
                     "wallBudgetS": 1800, "maxSubtasks": 4},
        "deep": {"theta": 8.5, "lambda_": 0.02, "maxIter": 6,
                 "tieBreak": "cost_then_latency",
                 "wallBudgetS": 1800, "maxSubtasks": 4},
    },
    # v15.4：模型参与动态化参数位（E-16/17/20）
    "verification": {
        "minVerifiers": 1,          # 每子任务验证者下限（可用异厂商不足时降到这里）
        "maxVerifiers": 3,          # 每子任务验证者上限（防呆，池子大时不拉爆）
        "maxExecutorsPerSubtask": 1,  # 执行者数参数位（双路互评时=2，由 dualExecTiers 控制）
        "dualExecTiers": ["standard", "deep"],  # 双路执行互评档位（fast 只做轮换）
    },
    # 熔断器（三态 + 指数退避 + 半开探测）
    "circuit": {
        "failureThreshold": 3,     # 窗口内连续失败数触发熔断
        "failureWindowS": 600,     # 10 分钟无新失败 → 计数清零
        "baseBackoffS": 300,       # 首次熔断冷却 5 分钟
        "maxBackoffS": 3600,       # 指数退避上限 1 小时
        "probeTtlS": 300,          # 半开探测 5 分钟无结果视为放弃
    },
    # 评分函数
    "selection": {
        "divBonus": 0.3,           # 厂商多样性加分
        "sampleKnee": 20,          # 样本拐点：samples 达到该值视为满置信
        "epsilon": 0.05,           # ε-greedy 探索概率（run 内默认）
        "mu": 0.001,               # 延迟惩罚系数（run 内默认；延迟数据来自 caps.runtime.latencyP50Ms 回填）
        "defaultCacheHitRate": 0.0,
        "paretoEnabled": True,     # P2-1：Pareto 前沿偏好（v15.4 打开——"没人比你又强又便宜就加分"）
        "paretoBonus": 0.1,        # 前沿候选的软加分
        "eloEnabled": True,        # P1-3：末轮 pairwise/ELO 横向比较（低分降权，不剔除）
        "eloPenaltyScale": 0.2,    # Elo 落后 100 分 ≈ 扣 0.2 选择分
        "allowlist": [],           # P0-2：候选池 allowlist（空=不启用；填 baseModel 白名单）
        "staticEligibilityPreFilter": True,  # P0-2：静态不合格候选（identityUnknown/stable=false/自验禁令）在评分前剔除，只记一条 summary 事件
        "rankNormalize": True,     # v15.3：能力分 rank 归一化（同质化治理，见 selector.build_rank_table）
    },
    # 终止策略
    "terminator": {
        "delta": 0.3,              # 边际收益早停阈值
        "maxRounds": 8,            # 全局防呆值（非档位参数）
    },
    # 流式活性超时（v15.4：total 900s——防一次卡死调用耗光统一后的 1800s run 墙钟）
    "streaming": {
        "idleTimeoutS": 180,
        "totalTimeoutS": 900,
    },
    # 运行期反馈 → 能力档案（贝叶斯融合）
    "feedback": {
        "sampleKnee": 20,          # 样本拐点：runtime 权重随样本数上升
        "minFlipRuns": 10,         # 翻排名门槛（有效 run 数）
        "minScoreChange": 0.5,     # 最小改动阈值：|Δ|<0.5 不写分只记样本（防噪声）
        "mergeWeight": 0.7,        # 融合时 runtime 分权重上限（w × mergeWeight）
        "zToScoreScale": 2.5,      # z-score → 分制偏移系数
        "autoApply": True,         # v15.4（the maintainer decided全程无人值守）：run 收尾统一写 pending diff，
                                   # 插件每日 04:30 漂移体检通过后自动 --apply（apply_pending 内自带
                                   # _drift_paused 体检拒绝 + baseHash 校验 + 写前校验三层安全网）
        "requireHeteroScorer": True,  # P0-5：写档案前强制 scoredBy ≠ 被评模型（同源样本拒绝）
    },
    # 成本对账（P0-3）
    "cost": {
        "reconcileAlertPct": 10.0, # |drift| > 10% → 写 cost-reconcile-events.jsonl 并告警
        "reconcileHour": "04:00",  # 每日北京时间对账时间（插件定时任务）
    },
    # benchmark 摄入
    "ingest": {
        "emaAlpha": 0.3,           # EMA 权重：新基准数据对当前分数的更新幅度
        "fallbackDimScore": 5.0,   # 极端兜底（无历史分数且均值异常时）
    },
    # 汇率陈旧语义（v15.4：黄灯/停机只作信息标记与成本置信度降级，不再停机拒绝运行）
    "fx": {
        "staleMaxHours": 26,       # 更新超过 26 小时（跨过一个工作日发布窗口）即过期
        "staleWarnDays": 1,        # 落后 ≥1 个交易日 → 黄灯（状态/事件提示）
        "staleHaltDays": 3,        # 落后 ≥3 个交易日 → 信息级标记（v15.4 起不再拒绝开跑）
    },
    # judge 漂移监控（金标集自评）
    "judgeDrift": {
        "enabled": True,
        "judgeModel": "MiniMax-M3",
        "judgeThinking": "medium",
        "judge2Model": "deepseek-v4-flash",  # P1-2：异源第二 judge 交叉验证
        "judge2Thinking": "low",
        "alertThreshold": 1.0,     # 金标集均分相对基线的偏差超过 1.0 分 → 告警
        "pauseWriteDrift": 0.5,    # P1-2：|drift| ≥ 0.5 → 暂停能力档案回写（update_capabilities 拒绝）
        "crossJudgeAlert": 1.0,    # 双 judge 均分差超过 1.0 → 交叉验证告警
        "dailyHour": "04:30",      # 每日北京时间低峰自评
        "baselineMinItems": 3,     # 基线至少 N 题有效才算数
    },
    # 墙钟 SLA（v15.4：成本目标降为纯观察，见 sla_stats.py 的观察模式）
    "sla": {
        "fast": {"wallP50S": 60, "wallP95S": 120, "costCapCny": None},
        "standard": {"wallP50S": 600, "wallP95S": 1200, "costCapCny": None},
        "deep": {"wallP50S": 1200, "wallP95S": 2400, "costCapCny": None},
    },
}


def _deep_merge(base: dict, override: dict) -> dict:
    out = dict(base)
    for k, v in (override or {}).items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _deep_merge(out[k], v)
        else:
            out[k] = v
    return out


def load() -> dict:
    """加载合并后的参数。文件缺失/损坏 → 返回 DEFAULTS 副本并附 _source 标记。"""
    if not PARAMS_FILE.exists():
        out = _deep_merge(DEFAULTS, {})
        out["_source"] = "defaults"
        return out
    try:
        raw = json.loads(PARAMS_FILE.read_text(encoding="utf-8"))
        if not isinstance(raw, dict):
            raise ValueError("params file root must be a dict")
        out = _deep_merge(DEFAULTS, raw)
        out["_source"] = str(PARAMS_FILE)
        return out
    except (json.JSONDecodeError, OSError, ValueError) as e:
        out = _deep_merge(DEFAULTS, {})
        out["_source"] = f"defaults(fallback: {type(e).__name__})"
        return out


def get(params: dict, dotted: str, default=None):
    """点路径取值：get(p, "circuit.failureThreshold")。"""
    cur = params
    for part in dotted.split("."):
        if not isinstance(cur, dict) or part not in cur:
            return default
        cur = cur[part]
    return cur


def circuit_params(params: dict = None) -> dict:
    return (params if params is not None else load())["circuit"]


if __name__ == "__main__":
    import sys
    if "--show" in sys.argv:
        print(json.dumps(load(), ensure_ascii=False, indent=2))
    else:
        print(f"params 来源：{load()['_source']}")
