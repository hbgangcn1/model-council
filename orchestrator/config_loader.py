"""Orchestrator 共用配置：凭证、档位映射（v15.5 起走 host-side tier-bridge）、端点、活性超时参数。"""
import re
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent  # council/
if str(BASE) not in sys.path:
    sys.path.insert(0, str(BASE))
import bridge  # noqa: E402  v15.5 单一数据源：host-side tier-bridge

DEEPSEEK_URL = "https://api.deepseek.com/chat/completions"
MINIMAX_URL = "https://api.minimaxi.com/anthropic/v1/messages"

IDLE_TIMEOUT_S = 180
TOTAL_TIMEOUT_S = 1800

# 全系统唯一时间口径：Asia/Shanghai（H3 时区契约；避免机器本地时区漂移）。
SHANGHAI_TZ = timezone(timedelta(hours=8), name="Asia/Shanghai")

def now_shanghai() -> datetime:
    return datetime.now(SHANGHAI_TZ)

def api_keys() -> dict:
    cred = Path.home() / ".dsh" / ".credentials.yaml"
    keys = {}
    for line in cred.read_text(encoding="utf-8").splitlines():
        m = re.match(r"\s*([A-Z_0-9]+):\s*(.+)\s*$", line)
        if m:
            keys[m.group(1)] = m.group(2).strip()
    return keys

def thinking_param(model: str, level: str) -> dict:
    """v15.5：档位 wire 拼写一律来自 host-side tier-bridge（与主会话同源），无手填映射。"""
    return bridge.wire_for(model, level)

def max_tokens_for_model(model: str, which: str = "defaultMaxTokens") -> int:
    """v15.5：max_tokens 取 host-bridge文件的模型上限（defaultMaxTokens=请求默认，
    capabilityMaxTokens=输出能力上限），不再按档位自定义 16384/8192。"""
    return bridge.max_tokens_for(model, which)
