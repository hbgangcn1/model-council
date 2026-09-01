"""金标升级「评卷考场」（v15.5-E-12）：为每条金标生成劣质答案变体（badAnswers）+
期望分（expectedScore）+ 机器判分锚标记（machineCheckable）。

坏答案用确定性规则模板生成（不依赖 AI，可复现）：
- 模板 A「答非所问」：通用低质回答，expectedScore 0.5；
- 模板 B「回答不完整」：截断好答案前半 + 省略说明，expectedScore 3.0；
- 模板 C「计算/数字错误」（仅 reasoning/code 类含数字的答案）：把第一个整数 +1，expectedScore 1.0。
好答案 expectedScore = 9.5（标准答案按 rubric 近满分）。
machineCheckable：reasoning/code/tool_use 维度（有客观答案/机器判分结构）标记 true。

安全契约：holdout=true 条目同样补字段（只读增强，不改变 task/answer/rubric 原内容）。
"""
import json
import re
import sys
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent  # council/
GOLDEN = BASE / "benchmark" / "golden" / "golden-set.json"

TEMPLATE_DODGE = ("这个问题的答案因人而异，没有唯一确定的标准，"
                  "建议结合具体场景咨询专业人士后自行判断。")
TEMPLATE_DODGE_LABEL = "答非所问"

MACHINE_DIMS = {"reasoning", "code", "tool_use"}


def _first_number_plus_one(text: str):
    """把答案中第一个整数 +1（生成计算错误变体）。"""
    m = re.search(r"(-?\d+)", text)
    if not m:
        return None
    val = int(m.group(1))
    return text[:m.start(1)] + str(val + 1) + text[m.end(1):]


def bad_variants(item: dict) -> list:
    dim = item.get("dimension", "")
    answer = item.get("answer", "")
    out = [
        {"label": TEMPLATE_DODGE_LABEL, "text": TEMPLATE_DODGE, "expectedScore": 0.5},
    ]
    if len(answer) > 12:
        cut = max(8, len(answer) // 2)
        out.append({"label": "回答不完整", "text": answer[:cut] + "……（其余内容略）",
                    "expectedScore": 3.0})
    if dim in ("reasoning", "code"):
        wrong = _first_number_plus_one(answer)
        if wrong and wrong != answer:
            out.append({"label": "计算/数字错误", "text": wrong, "expectedScore": 1.0})
    return out


def main():
    doc = json.loads(GOLDEN.read_text(encoding="utf-8"))
    items = doc.get("items", [])
    changed = 0
    for it in items:
        if "expectedScore" not in it:
            it["expectedScore"] = 9.5
            changed += 1
        if "badAnswers" not in it:
            it["badAnswers"] = bad_variants(it)
            changed += 1
        if "machineCheckable" not in it:
            it["machineCheckable"] = it.get("dimension") in MACHINE_DIMS
            changed += 1
    doc["_comment"] = (doc.get("_comment", "") +
                       " | v15.5 评卷考场：expectedScore(好答案期望分)/badAnswers(劣质变体+期望分)/"
                       "machineCheckable(机器判分锚标记，reasoning/code/tool_use)")
    # contentHash 重算（golden_guard.items_hash 同算法）
    import hashlib
    doc["contentHash"] = hashlib.sha256(
        json.dumps(items, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()
    tmp = GOLDEN.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(doc, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(GOLDEN)
    machine = sum(1 for it in items if it.get("machineCheckable"))
    print(f"✅ 增强完成：{len(items)} 条，字段变更 {changed} 条，"
          f"machineCheckable {machine}/{len(items)}（{machine / len(items) * 100:.0f}%）")
    if machine / len(items) < 0.25:
        print("⚠ machineCheckable 占比 < 25%（A1 锚保底线），建议补充 reasoning/code 金标",
              file=sys.stderr)


if __name__ == "__main__":
    main()
