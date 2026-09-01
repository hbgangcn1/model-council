"""案例加载 + 长文素材注入。"""
import json
from . import config

# 长文题：把素材内容拼进 prompt（题面中的占位说明替换为真实素材）
_LONGCTX_SOURCES = {
    "L1": "l1.md",
    "L3": "l3-code.md",
    "L4": "l4.md",
}
# 工具题：沙箱目录映射
_TOOL_SANDBOX = {
    "T1": "t1",
    "T2": "t2",
    "T3": "t3",
}

def load_cases():
    cases = json.loads(config.CASES_FILE.read_text(encoding="utf-8"))["cases"]
    return cases

def build_prompt(case: dict) -> str:
    """组装发给模型的 prompt：长文题注入素材。"""
    prompt = case["prompt"]
    cid = case["id"]
    if cid in _LONGCTX_SOURCES:
        src = (config.LONGCTX_DIR / _LONGCTX_SOURCES[cid]).read_text(encoding="utf-8")
        prompt = f"{prompt}\n\n--- 附：以下是要分析的正文材料 ---\n{src}"
    return prompt

def needs_tools(case: dict) -> bool:
    return case["id"] in _TOOL_SANDBOX

def sandbox_root_for(case: dict):
    cid = case["id"]
    if cid not in _TOOL_SANDBOX:
        return None
    return config.SANDBOX_DIR / _TOOL_SANDBOX[cid]

def dimension_of(case: dict) -> str:
    return case["dimension"]
