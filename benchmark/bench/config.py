"""基准配置：凭证加载、候选条目（v15.5 起全档位×全案例，来自 host-side tier-bridge）。"""
import json
import re
import sys
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent  # benchmark/
COUNCIL = BASE.parent  # council/
if str(COUNCIL) not in sys.path:
    sys.path.insert(0, str(COUNCIL))
import bridge  # noqa: E402  v15.5 单一数据源：host-side tier-bridge

CASES_FILE = BASE / "v21-cases.json"
LONGCTX_DIR = BASE / "longctx-content"
SANDBOX_DIR = BASE / "test-sandbox"
RESPONSES_DIR = BASE / "responses"
SCORES_DIR = BASE / "scores"
PROGRESS_FILE = BASE / "progress.json"
REPORT_FILE = BASE / "report.md"

# --- 凭证（从 host credentials 读，绝不硬编码） ---
_CRED = Path.home() / ".dsh" / ".credentials.yaml"

def load_api_keys() -> dict:
    text = _CRED.read_text(encoding="utf-8")
    keys = {}
    for line in text.splitlines():
        m = re.match(r"\s*([A-Z_0-9]+):\s*(.+)\s*$", line)
        if m:
            keys[m.group(1)] = m.group(2).strip()
    return keys

# --- 候选条目（v15.5：全档位 × 全案例——每模型全部 host catalog档位；插值已废除） ---
CANDIDATES = bridge.all_candidates()

def cand_id(model: str, thinking: str) -> str:
    """文件系统安全名。"""
    return model.replace("/", "--") + "__" + thinking

def parse_cand_id(cid: str):
    model, thinking = cid.rsplit("__", 1)
    return model.replace("--", "/"), thinking

# --- 档位 → API wire（v15.5：来自 host-side tier-bridge，与主会话同源；删手填 budget_tokens 映射） ---
def thinking_param(model: str, level: str) -> dict:
    return bridge.wire_for(model, level)

def max_tokens_for(model: str) -> int:
    """v15.5：max_tokens = 模型输出能力上限（capabilityMaxTokens），
    不再按档位自定义 32768/8192（实测 flash__high 单题思考 27.5K 会被旧值截断）。"""
    return bridge.max_tokens_for(model, "capabilityMaxTokens")

# --- 端点 ---
DEEPSEEK_URL = "https://api.deepseek.com/chat/completions"
MINIMAX_URL = "https://api.minimaxi.com/anthropic/v1/messages"

def save_json(path: Path, obj):
    """原子写：临时文件 + rename。"""
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(obj, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(path)
