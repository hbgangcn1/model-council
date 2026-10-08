"""DSH 模型档位桥（model-tier-bridge.json）加载——v15.5 单一数据源。

档位枚举、wire 拼写、maxTokens 一律来自 DSH 运行时模型目录（插件桥 0.5 自动生成；
本手工版内容与 DSH 目录核对一致）。fail-loud：模型不在桥中或档位不存在直接抛错，
绝不静默回退手填映射（tier-alignment-plan.md §1.3）。
"""
import json
from pathlib import Path

BASE = Path(__file__).resolve().parent
BRIDGE_FILE = BASE / "model-tier-bridge.json"

_cache = None


def load() -> dict:
    global _cache
    if _cache is not None:
        return _cache
    if not BRIDGE_FILE.exists():
        raise RuntimeError(f"缺模型档位桥 {BRIDGE_FILE}（fail-loud：请先由 dsh-council 插件生成）")
    doc = json.loads(BRIDGE_FILE.read_text(encoding="utf-8"))
    _cache = doc
    return doc


def _norm(model: str) -> str:
    """档案 cid 把 '/' 编码为 '--'（build_capabilities 的 cand_id），
    桥文件用原始模型名——查表前还原。"""
    return model.replace('--', '/', 1) if '--' in model else model


def model_entry(model: str) -> dict:
    doc = load()
    entry = doc.get("models", {}).get(model)
    if entry is None:
        entry = doc.get("models", {}).get(_norm(model))
    if entry is None:
        raise ValueError(f"模型 {model} 不在 DSH 档位桥中（fail-loud：请检查 DSH 模型配置）")
    return entry


def wire_for(model: str, level: str) -> dict:
    entry = model_entry(model)
    for lv in entry.get("levels", []):
        if lv.get("level") == level:
            return lv.get("wire", {})
    raise ValueError(f"模型 {model} 无档位 {level}（桥文件 levels={[lv['level'] for lv in entry.get('levels', [])]}）")


def levels_for(model: str) -> list:
    return [lv["level"] for lv in model_entry(model).get("levels", [])]


def max_tokens_for(model: str, which: str = "capabilityMaxTokens") -> int:
    entry = model_entry(model)
    val = entry.get(which) or entry.get("capabilityMaxTokens")
    if not val:
        raise ValueError(f"模型 {model} 桥条目缺 {which}")
    return int(val)


def vendor_group(model: str) -> str:
    return str(model_entry(model).get("vendorGroup") or "")


def all_candidates() -> list:
    """(model, level) 全档位枚举（全档位×全案例，v15.5）。"""
    out = []
    for m in load().get("models", {}):
        for lv in levels_for(m):
            out.append((m, lv))
    return out
