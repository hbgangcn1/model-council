"""capabilities.json 写前校验（评审报告 H 项：写前校验，防坏数据落盘）。

validate(doc, old_revision=None) 抛 ValueError 的情形：
- 结构不是 dict / models 不是 dict / models 为空；
- revision 非正整数，或写前校验时新 revision < 旧 revision（回退拒绝）；
- 维度分数不是 None/数字，或越出 [0,10]；
- dimensions 与 schemaVersion 2 的九维契约不符（缺维报错，多维亚历式放行）。

任何写入方（update_capabilities / capability_ingest / build_capabilities / UI settings 写回）
都应在 tmp.replace 前调用一次——坏档案一旦落盘，selector 全会读错。
"""
from pathlib import Path

EXPECTED_DIMENSIONS = ["reasoning", "code", "chinese", "research",
                       "instruction_following", "long_context",
                       "tool_use", "creativity", "safety"]


def validate(doc: dict, old_revision: int = None, require_dimensions: bool = True) -> list:
    """返回空列表=通过；否则返回问题描述列表（不抛异常，方便调用方聚合报告）。"""
    problems = []
    if not isinstance(doc, dict):
        return ["capabilities 根必须是 dict"]
    models = doc.get("models")
    if not isinstance(models, dict) or not models:
        problems.append("models 缺失或为空 dict（档案没有候选条目）")
        return problems
    rev = doc.get("revision")
    if not isinstance(rev, int) or isinstance(rev, bool) or rev < 0:
        problems.append(f"revision 必须是非负整数，当前={rev!r}")
    elif old_revision is not None and rev < old_revision:
        problems.append(f"revision 回退拒绝：新={rev} < 旧={old_revision}")
    if require_dimensions:
        dims = doc.get("dimensions")
        # 字段缺失时跳过（最小档案/程序化构造场景）；存在但缺契约维度 → 拒绝
        if isinstance(dims, list) and dims and any(d not in dims for d in EXPECTED_DIMENSIONS):
            problems.append(f"dimensions 缺少契约维度（需含 {EXPECTED_DIMENSIONS}）")
    for cid, model in models.items():
        if not isinstance(model, dict):
            problems.append(f"{cid}: 条目不是 dict")
            continue
        if not isinstance(model.get("baseModel"), str) or not model["baseModel"]:
            problems.append(f"{cid}: baseModel 缺失")
        caps = model.get("capabilities")
        if caps is None:
            continue
        if not isinstance(caps, dict):
            problems.append(f"{cid}: capabilities 不是 dict")
            continue
        for dim, entry in caps.items():
            if not isinstance(entry, dict):
                problems.append(f"{cid}.{dim}: 条目不是 dict")
                continue
            score = entry.get("score")
            if score is None:
                continue
            if not isinstance(score, (int, float)) or isinstance(score, bool):
                problems.append(f"{cid}.{dim}: score 不是数字（{score!r}）")
                continue
            if not (0.0 <= float(score) <= 10.0):
                problems.append(f"{cid}.{dim}: score={score} 越出 [0,10]")
    return problems


def validate_or_raise(doc: dict, old_revision: int = None, source: str = "caps") -> None:
    problems = validate(doc, old_revision=old_revision)
    if problems:
        raise ValueError(f"{source} 写前校验失败：{'；'.join(problems[:5])}")


def validate_file(path: Path, old_revision: int = None) -> dict:
    import json
    doc = json.loads(path.read_text(encoding="utf-8"))
    validate_or_raise(doc, old_revision=old_revision, source=path.name)
    return doc
