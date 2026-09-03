"""Orchestrator 共用配置：凭证、档位映射（v15.5 起走 host-side tier-bridge）、端点、活性超时参数。"""
import os
import re
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

from . import tier_bridge  # v15.5 单一数据源：host-side tier-bridge

DEEPSEEK_URL = "https://api.deepseek.com/chat/completions"
MINIMAX_URL = "https://api.minimaxi.com/anthropic/v1/messages"

IDLE_TIMEOUT_S = 180
TOTAL_TIMEOUT_S = 1800

# 全系统唯一时间口径：Asia/Shanghai（H3 时区契约；避免机器本地时区漂移）。
SHANGHAI_TZ = timezone(timedelta(hours=8), name="Asia/Shanghai")

def now_shanghai() -> datetime:
    return datetime.now(SHANGHAI_TZ)

# 环境变量直读的 key 名（新增 provider 时往这加一行即可；
# 实际消费方：query_balance 用前两个，llm_client/各 transport 用 COMPAT/BRIDGE）。
_KNOWN_KEY_NAMES = (
    "DEEPSEEK_API_KEY",
    "MINIMAX_CN_API_KEY",
    "OPENAI_COMPAT_BASE",
    "OPENAI_COMPAT_KEY",
    "ANTHROPIC_COMPAT_BASE",
    "ANTHROPIC_COMPAT_KEY",
    "DSH_BRIDGE_URL",
)


def _read_simple_kv_file(path: Path) -> dict:
    keys = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        m = re.match(r"\s*([A-Z_0-9]+):\s*(.+)\s*$", line)
        if m:
            keys[m.group(1)] = m.group(2).strip()
    return keys


def api_keys() -> dict:
    """v15.9 四级凭证来源（优先级从高到低，本地 DSH 用户零改动兼容）：

    1. 环境变量 MODEL_COUNCIL_CREDENTIALS 指向的 KEY: value 文件（显式指定，
       文件不存在直接报错，不静默 fallback）；
    2. 环境变量直读（容器/CI 友好，无文件可跑；会覆盖文件里的同名项）；
    3. ~/.model-council/credentials（非 DSH 用户的标准位置）；
    4. ~/.dsh/.credentials.yaml（DSH 本地惯例，保留兜底）。
    都找不到时抛 FileNotFoundError 并告诉用户三选一怎么做。
    """
    keys: dict = {}
    env_file = os.environ.get("MODEL_COUNCIL_CREDENTIALS", "").strip()
    if env_file:
        p = Path(env_file)
        if not p.is_file():
            raise FileNotFoundError(
                f"MODEL_COUNCIL_CREDENTIALS points to missing file: {env_file}")
        return _read_simple_kv_file(p)
    for cand in (Path.home() / ".model-council" / "credentials",
                 Path.home() / ".dsh" / ".credentials.yaml"):
        try:
            if cand.is_file():
                keys.update(_read_simple_kv_file(cand))
                break  # 只取第一个存在的文件
        except OSError:
            continue
    for name in _KNOWN_KEY_NAMES:
        val = os.environ.get(name, "").strip()
        if val:
            keys[name] = val
    if not keys:
        raise FileNotFoundError(
            "no API credentials found; pick one: "
            "export DEEPSEEK_API_KEY=... (and/or other *_KEY vars), "
            "or set MODEL_COUNCIL_CREDENTIALS=/path/to/credentials, "
            "or create ~/.model-council/credentials (KEY: value per line)")
    return keys

def thinking_param(model: str, level: str) -> dict:
    """v15.5：档位 wire 拼写一律来自 host-side tier-bridge（与主会话同源），无手填映射。"""
    return tier_bridge.wire_for(model, level)

def max_tokens_for_model(model: str, which: str = "defaultMaxTokens") -> int:
    """v15.5：max_tokens 取 host-bridge文件的模型上限（defaultMaxTokens=请求默认，
    capabilityMaxTokens=输出能力上限），不再按档位自定义 16384/8192。"""
    return tier_bridge.max_tokens_for(model, which)
