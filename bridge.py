"""DSH 模型档位桥 (dev) — 三级 fallback 版本。

干净 checkout + `pip install -e .` 后,benchmark/bench/config.py 第 11 行 `import bridge`
不应崩。本文件是开发期入口,行为契约与运行时 ~/.dsh/council/bridge.py 一致:

  - all_candidates() -> list[(model, level)]
  - wire_for(model, level) -> dict
  - max_tokens_for(model, which="capabilityMaxTokens") -> int
  - levels_for(model) -> list[str]
  - model_entry(model) -> dict
  - vendor_group(model) -> str

加载顺序(全部 fail-loud,不静默回退):
  1. PATH_BRIDGE_FILE 环境变量(显式 override)
  2. ~/.dsh/council/model-tier-bridge.json(运行时,若有 DSH 安装)
  3. {repo_root}/benchmark/test-fixtures/model-tier-bridge.json(dev 最小 fixture)

约束:不动运行时 ~/.dsh/council/(本文件是 dev 副本,运行时副本独立维护)。
"""
import json
import os
from pathlib import Path

BASE = Path(__file__).resolve().parent  # dev repo root

RUNTIME_BRIDGE = Path.home() / ".dsh" / "council" / "model-tier-bridge.json"
DEV_FIXTURE = BASE / "benchmark" / "test-fixtures" / "model-tier-bridge.json"

_cache = None


def _resolve_source() -> Path:
    """Pick the first existing bridge JSON. Fail loud if none."""
    override = os.environ.get("PATH_BRIDGE_FILE")
    if override:
        p = Path(override)
        if p.exists():
            return p
    if RUNTIME_BRIDGE.exists():
        return RUNTIME_BRIDGE
    if DEV_FIXTURE.exists():
        return DEV_FIXTURE
    raise RuntimeError(
        f"缺 model-tier-bridge.json (尝试过: override={override!r}, "
        f"runtime={RUNTIME_BRIDGE}, dev_fixture={DEV_FIXTURE}). "
        "处理方式: (a) 设置 PATH_BRIDGE_FILE 指向已有桥文件; "
        "(b) 跑 dsh-council 插件生成运行时 ~/.dsh/council/model-tier-bridge.json; "
        "(c) 检查 dev 仓 benchmark/test-fixtures/model-tier-bridge.json 是否存在。"
    )


def load() -> dict:
    global _cache
    if _cache is not None:
        return _cache
    src = _resolve_source()
    try:
        doc = json.loads(src.read_text(encoding="utf-8"))
    except json.JSONDecodeError as e:
        raise RuntimeError(f"桥文件 {src} JSON 解析失败: {e}") from e
    if not isinstance(doc, dict) or "models" not in doc:
        raise RuntimeError(f"桥文件 {src} 不是合法 JSON dict (缺 'models' 键)")
    _cache = doc
    return doc


def _norm(model: str) -> str:
    """档案 cid 把 '/' 编码为 '--' (build_capabilities 的 cand_id),
    桥文件用原始模型名——查表前还原。"""
    return model.replace("--", "/", 1) if "--" in model else model


def model_entry(model: str) -> dict:
    doc = load()
    entry = doc.get("models", {}).get(model)
    if entry is None:
        entry = doc.get("models", {}).get(_norm(model))
    if entry is None:
        raise ValueError(
            f"模型 {model!r} 不在 DSH 档位桥中 "
            "(fail-loud: 检查 DSH 模型配置或设置 PATH_BRIDGE_FILE)"
        )
    return entry


def wire_for(model: str, level: str) -> dict:
    entry = model_entry(model)
    levels = entry.get("levels") or []
    for lv in levels:
        if isinstance(lv, dict) and lv.get("level") == level:
            return lv.get("wire") or {}
    raise ValueError(
        f"模型 {model} 无档位 {level} "
        f"(桥文件 levels={[lv.get('level') for lv in levels if isinstance(lv, dict)]})"
    )


def levels_for(model: str) -> list:
    return [
        lv["level"]
        for lv in model_entry(model).get("levels", [])
        if isinstance(lv, dict) and "level" in lv
    ]


def max_tokens_for(model: str, which: str = "capabilityMaxTokens") -> int:
    entry = model_entry(model)
    val = entry.get(which) or entry.get("capabilityMaxTokens")
    if not val:
        raise ValueError(f"模型 {model} 桥条目缺 {which}")
    return int(val)


def vendor_group(model: str) -> str:
    return str(model_entry(model).get("vendorGroup") or "")


def all_candidates() -> list:
    """(model, level) 全档位枚举。"""
    out = []
    for m in load().get("models", {}):
        for lv in levels_for(m):
            out.append((m, lv))
    return out
