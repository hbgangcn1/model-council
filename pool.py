"""模型池名单（model-pool.json）——v15.5-K 成员准入第一层。

- 池成员 = model 粒度名单（全档位按 host-side tier-bridge展开）。
- 增=手动（控制台/端点写）、删=自动为主（DSH 删模型自动退役）+ 手动删除。
- build_capabilities 只为池成员生成档案条目；未跑分成员天然无档案条目 → 不参与选择。
- 文件缺失时按「桥文件全模型 active」初始化（向后兼容迁移：现有模型全入池）。
"""
import json
import time
from pathlib import Path

BASE = Path(__file__).resolve().parent
POOL_FILE = BASE / "model-pool.json"

_cache = None
_cache_mtime = None


def load() -> dict:
    global _cache, _cache_mtime
    if POOL_FILE.exists():
        mtime = POOL_FILE.stat().st_mtime
        if _cache is None or _cache_mtime != mtime:
            _cache = json.loads(POOL_FILE.read_text(encoding="utf-8"))
            _cache_mtime = mtime
        return _cache
    # 向后兼容：桥文件全模型 active
    import bridge
    doc = {"schemaVersion": 1,
           "models": [{"model": m, "status": "active",
                       "addedAt": None, "note": "migrated-from-bridge"} for m in bridge.load()["models"]]}
    _cache = doc
    return doc


def members() -> list:
    """active 池成员（model 名）。"""
    return [m["model"] for m in load().get("models", [])
            if m.get("status") == "active"]


def _norm(model: str) -> str:
    """档案 cid 把 '/' 编码为 '--'，池名单用原始模型名——查表前还原。"""
    return model.replace('--', '/', 1) if '--' in model else model


def is_member(model: str) -> bool:
    return model in members() or _norm(model) in members()


def add(model: str, note: str = "") -> dict:
    """手动加入池（控制台操作，插件 HTTP 层调用同逻辑时以 node 实现为准）。"""
    doc = load()
    for m in doc.get("models", []):
        if m["model"] == model:
            m["status"] = "active"
            return doc
    doc.setdefault("models", []).append(
        {"model": model, "status": "active", "addedAt": time.time(), "note": note})
    return doc


def remove(model: str, reason: str) -> dict:
    """从池移除（retired-by-user / retired-by-dsh-removal）。成绩保留只读。"""
    doc = load()
    for m in doc.get("models", []):
        if m["model"] == model:
            m["status"] = reason
            return doc
    doc.setdefault("models", []).append(
        {"model": model, "status": reason, "addedAt": time.time(),
         "note": f"retired: {reason}"})
    return doc


def save(doc: dict):
    tmp = POOL_FILE.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(doc, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(POOL_FILE)
