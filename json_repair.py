"""公共 lenient JSON 解析（v15.5-D-11 四层防线第一层）。

模型输出 JSON 的常见损坏模式与修复（golden_evolve 经验推广到 verdict/decompose）：
1. fence 包裹：```json ... ``` → 优先剥 fence；
2. 字符串内裸换行（ox-alpha 实测）：逐字符状态机转义（regex 无法区分结构换行与字符串换行）；
3. 未转义引号启发式（保守）：修复 `"key": "text "quote" text"` 中字符串内引号；
4. 截断修复：截到最后一个完整右花括号 / 补全缺失右括号。

extract_verdict_fields()：整体 JSON 仍失败时的关键字段降级抠取（防线第二层）——
overallScore / hardGateFailed / reworkList 拿到分数即可继续参与收敛。
"""
import json
import re


def _fix_newlines_in_strings(s: str) -> str:
    """状态机修复：只把 JSON 字符串值内部的裸换行转义为 \\n（结构换行保留）。"""
    out = []
    in_str = False
    esc = False
    for ch in s:
        if in_str:
            if ch == "\n":
                out.append("\\n")
                continue
            out.append(ch)
            if ch == "\\" and not esc:
                esc = True
            elif esc:
                esc = False
            elif ch == '"':
                in_str = False
        else:
            out.append(ch)
            if ch == '"':
                in_str = True
    return "".join(out)


def _fix_unescaped_quotes(s: str) -> str:
    """保守启发式：`"value" 中间 " 又 " 引号` 模式 → 转义内部引号。
    仅在简单 JSON 解析失败后调用，按 `"..."` 值内出现 ` " ` 后还有 `"` 的模式修复。"""
    out = []
    in_str = False
    esc = False
    i = 0
    n = len(s)
    while i < n:
        ch = s[i]
        if in_str:
            if ch == "\\" and not esc:
                esc = True
                out.append(ch)
                i += 1
                continue
            if esc:
                esc = False
                out.append(ch)
                i += 1
                continue
            if ch == '"':
                # 判断是否字符串结束：后一个非空白字符是 , } ] : 或行尾 → 结束；否则是内部引号
                j = i + 1
                while j < n and s[j] in " \t\r\n":
                    j += 1
                nxt = s[j] if j < n else ""
                if nxt in (",", "}", "]", ":", "") or nxt in "}" :
                    in_str = False
                    out.append(ch)
                else:
                    out.append("\\\"")
                i += 1
                continue
            out.append(ch)
            i += 1
            continue
        out.append(ch)
        if ch == '"':
            in_str = True
        i += 1
    return "".join(out)


def _truncate_repair(s: str) -> str:
    """截断修复：①找最后一个完整顶层对象（大括号平衡归零处）截断；②截断未闭合时
    按开括号栈反向补全 `]`/`}`（数组与对象都处理）。"""
    depth = 0
    last_good = -1
    for i, ch in enumerate(s):
        if ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                last_good = i
    if last_good > 0:
        return s[:last_good + 1]
    # 补全：从首个 '{' 起扫描括号栈（忽略字符串内括号）
    start = s.find("{")
    if start < 0:
        return s
    stack = []
    in_str = False
    esc = False
    for ch in s[start:]:
        if in_str:
            if ch == "\\" and not esc:
                esc = True
            elif esc:
                esc = False
            elif ch == '"':
                in_str = False
            continue
        if ch == '"':
            in_str = True
        elif ch in "{[":
            stack.append(ch)
        elif ch in "}]":
            if stack and ((ch == "}" and stack[-1] == "{") or (ch == "]" and stack[-1] == "[")):
                stack.pop()
    closers = {"{": "}", "[": "]"}
    return s + "".join(closers[b] for b in reversed(stack))


def parse(text: str):
    """lenient JSON 解析：fence 剥离 → 裸换行修复 → 直接解析 → 截断修复 → 未转义引号修复。
    返回 parsed dict 或 None。"""
    if not text:
        return None
    raw = text
    m = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", text or "", re.S)
    if m:
        raw = m.group(1)
    else:
        m = re.search(r"\{.*\}", text, re.S)
        if m:
            raw = m.group(0)
        else:
            # 截断文本（无闭合 }）：取首个 { 到文末
            i = text.find("{")
            if i < 0:
                return None
            raw = text[i:]
    fixed = _fix_newlines_in_strings(raw)
    try:
        return json.loads(fixed)
    except json.JSONDecodeError:
        pass
    try:
        return json.loads(_truncate_repair(fixed))
    except json.JSONDecodeError:
        pass
    try:
        return json.loads(_fix_unescaped_quotes(fixed))
    except json.JSONDecodeError:
        pass
    return None


_VERDICT_FIELDS = {
    "overallScore": re.compile(r'"overallScore"\s*:\s*([0-9.]+)'),
    "hardGateFailed": re.compile(r'"hardGateFailed"\s*:\s*(true|false)'),
    "hardGateReasons": re.compile(r'"hardGateReasons"\s*:\s*(\[[^\]]*\])', re.S),
    "rationale": re.compile(r'"rationale"\s*:\s*"((?:[^"\\]|\\.)*)"', re.S),
}


def extract_verdict_fields(text: str) -> dict:
    """防线第二层：整体 JSON 坏了时，正则抠关键字段（verdict 用）。
    拿不到 score 返回 None（该 verifier 仍算失败，走重试/替补）。"""
    score_m = _VERDICT_FIELDS["overallScore"].search(text or "")
    if not score_m:
        return None
    out = {"overallScore": float(score_m.group(1)),
           "hardGateFailed": False, "hardGateReasons": [], "reworkList": [],
           "rationale": "", "_partial": True}
    gate_m = _VERDICT_FIELDS["hardGateFailed"].search(text or "")
    if gate_m:
        out["hardGateFailed"] = (gate_m.group(1) == "true")
    reasons_m = _VERDICT_FIELDS["hardGateReasons"].search(text or "")
    if reasons_m:
        try:
            out["hardGateReasons"] = json.loads(reasons_m.group(1))
        except json.JSONDecodeError:
            out["hardGateReasons"] = [reasons_m.group(1)[:200]]
    rat_m = _VERDICT_FIELDS["rationale"].search(text or "")
    if rat_m:
        out["rationale"] = rat_m.group(1)[:300]
    # reworkList 尽力抠（数组可能跨行）
    rw_m = re.search(r'"reworkList"\s*:\s*(\[.*\])', text or "", re.S)
    if rw_m:
        try:
            rw = json.loads(_fix_newlines_in_strings(rw_m.group(1)))
            if isinstance(rw, list):
                out["reworkList"] = rw
        except json.JSONDecodeError:
            pass
    return out
