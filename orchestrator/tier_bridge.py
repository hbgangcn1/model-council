"""orchestrator 侧的档位桥入口（v15.12g，2026-09-14 Robert 拍板方案 A）。

## 历史与病根
本文件此前一直是公开版桩（原文首行：“Tier-bridge stub for the public release”），
它**静默兜底**：未知模型一律给 8192 max_tokens（字典里只有改名前的旧 id），
非 deepseek 一律套 minimax 家族的 wire。

实测后果（run 2026-09-14_11-55-57 的 3 次首次失败 + 同日复现实验）：
  - `deepseek-flash`（2026-09-13 官方改名后的新名字）不在桩的字典里
    → 每次调用只有 8192 输出预算 → `@max` 档推理直接吃光（reasoning 事件 8192 个、
      outputTokens 8192、`finish=max-tokens`、text 事件 0 个）
    → 桥接抛 `DSH bridge completed response with no content`（空响应）。
  - `muse-spark-*` 被 `_family()` 当成 minimax → 发出 `budget_tokens` 格式的错误 wire。
而跑分链（`benchmark/bench/config.py`、`build_capabilities.py`、`pool.py` 等 5 处
`import bridge`）用的是**真桥** `model-tier-bridge.json`（v15.5 单一数据源 + fail-loud），
于是出现“跑分给 256000、运行给 8192”的双轨不一致。

## 现在
委托真桥，与跑分链对齐（单一数据源）。真桥对未知模型/未知档位**抛错**（fail-loud），
这是设计本意，不要在这里加静默回退。

仅当真桥文件缺失/不可读（例如把 council 目录搬到没有 `model-tier-bridge.json` 的机器）时，
才退回本文件内置的兜底表；兜底表已按真桥口径修正（各档走 reasoning_effort），
原因记在 `_FALLBACK_REASON` 里供排查。

契约（与 `bridge.py` 一致）：
  - `wire_for(model, level) -> dict`
  - `max_tokens_for(model, which) -> int`，which ∈ {defaultMaxTokens, capabilityMaxTokens}
"""
from __future__ import annotations

import importlib.util
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent  # council 根目录（bridge.py 所在）

_FALLBACK_REASON = None
_REAL = None
try:
    _spec = importlib.util.spec_from_file_location("_council_real_tier_bridge",
                                                   _ROOT / "bridge.py")
    if _spec is None or _spec.loader is None:
        raise RuntimeError("无法为 bridge.py 构造 import spec")
    _mod = importlib.util.module_from_spec(_spec)
    _spec.loader.exec_module(_mod)
    _REAL = _mod
except Exception as _e:  # noqa: BLE001 —— 任何加载失败都退回内置兜底表
    _REAL = None
    _FALLBACK_REASON = f"{type(_e).__name__}: {_e}"


# ---- 兜底表（仅在真桥不可用时使用；口径已与 model-tier-bridge.json 对齐） ----
_LEVELS = ("off", "minimal", "low", "medium", "high", "max")

_FALLBACK_MAX_TOKENS = {
    "deepseek-flash": 256000,
    "deepseek-v4-flash": 256000,
    "deepseek-v4-pro": 256000,
    "muse-spark-1.3-contributor-free": 131072,
    "MiniMax-M3": 131072,
    "MiniMax-M2.7": 131072,
}


def _fallback_wire(level: str) -> dict:
    if level == "off":
        return {"thinking": {"type": "disabled"}}
    if level in _LEVELS:
        return {"thinking": {"type": "enabled"}, "reasoning_effort": level}
    return {"thinking": {"type": "enabled"}, "level": level}


def wire_for(model: str, level: str) -> dict:
    """档位 → wire 参数。真桥可用时 fail-loud（模型不在桥中/无该档位 → 抛错）。"""
    if _REAL is not None:
        return _REAL.wire_for(model, level)
    return _fallback_wire(level)


def max_tokens_for(model: str, which: str = "defaultMaxTokens") -> int:
    """max_tokens。which='defaultMaxTokens'（请求默认）或 'capabilityMaxTokens'（输出能力上限）。"""
    if _REAL is not None:
        return _REAL.max_tokens_for(model, which)
    if which == "capabilityMaxTokens":
        return _FALLBACK_MAX_TOKENS.get(model, 16384)
    return _FALLBACK_MAX_TOKENS.get(model, 8192)


def levels_for(model: str) -> list:
    """该模型可用档位（真桥优先；兜底表给通用档位集合）。"""
    if _REAL is not None:
        return _REAL.levels_for(model)
    return list(_LEVELS)
