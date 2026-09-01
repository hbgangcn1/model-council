"""CostContext：成本计算上下文（H3 单位契约）。

所有字段带单位后缀，杜绝「裸值 96 是 96% 还是 0.96」这类歧义：
- 价格：*CnyPerMTok（每百万 token 人民币）
- token 数：*Tokens（个）
- 比例：timeFactor / quotaFactor / thinkingMult / cacheHitRate（无量纲倍数）
- 金额：costCny（元）/ costUsd（美元）
"""
from dataclasses import dataclass


@dataclass
class CostContext:
    provider: str
    model: str
    thinking: str
    inputCnyPerMTok: dict | None
    outputCnyPerMTok: dict | None
    cacheInputCnyPerMTok: dict | None
    timeFactor: float
    quotaFactor: float
    thinkingMult: float
    estInputTokens: int
    estOutputTokens: int
    cacheHitRate: float = 0.0
    costCny: float | None = None
    baseCostCny: float | None = None
    usdToCny: float | None = None
    currency: str = "CNY"

    @property
    def costUsd(self) -> float | None:
        """按当前汇率换算的美元成本（供 feedback_ring / 对账使用）。"""
        if self.costCny is None or self.costCny == float("inf"):
            return self.costCny
        if not self.usdToCny:
            return None
        return round(self.costCny / self.usdToCny, 6)

    def as_dict(self) -> dict:
        """对外契约（snake_case，与 selector/score_candidate 既有字段一致）。"""
        return {
            "provider": self.provider,
            "model": self.model,
            "thinking": self.thinking,
            "inputCnyPerMTok": self.inputCnyPerMTok,
            "outputCnyPerMTok": self.outputCnyPerMTok,
            "cacheInputCnyPerMTok": self.cacheInputCnyPerMTok,
            "time_factor": self.timeFactor,
            "quota_factor": self.quotaFactor,
            "thinking_mult": self.thinkingMult,
            "est_input_tokens": self.estInputTokens,
            "est_output_tokens": self.estOutputTokens,
            "cache_hit_rate": self.cacheHitRate,
            "cost_cny": self.costCny,
            "base_cost_cny": self.baseCostCny,
            "costUsd": self.costUsd,
            "usdToCny": self.usdToCny,
            "currency": self.currency,
        }
