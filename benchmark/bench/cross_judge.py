"""交叉评：7 道主观题。judge 与被评者 baseModel 永远互斥。

v15.2（元评审 P0-5/P2-3 同源加固）：
- _base_name 先剥 "provider/" 前缀再判家族——防 'minimax-portal/MiniMax-M3' 这类
  带前缀写法使 startswith("MiniMax") 互斥失效（M3 自评）；
- score_case 对 judge==被评者 fail loud（前置断言，双保险）。
"""
import json
import re

from . import llm

CROSS_CASES = {"C3", "CN1", "CN2", "CN3", "CR1", "CR2", "S2"}

def _base_name(model: str) -> str:
    """剥掉 'provider/' 前缀（如 'minimax-portal/MiniMax-M3' → 'MiniMax-M3'）。"""
    return (model or "").split("/")[-1]

def _judge_for(target_model: str, round_no: int = 1) -> str:
    """被评者是 deepseek 系 → judge 用 MiniMax；反之用 deepseek。复核轮换档位。"""
    if _base_name(target_model).startswith("MiniMax"):
        return "deepseek-v4-flash" if round_no == 1 else "deepseek-v4-pro"
    return "MiniMax-M3"

def _judge_thinking(judge_model: str, round_no: int = 1) -> str:
    if _base_name(judge_model).startswith("MiniMax"):
        return "off" if round_no == 1 else "medium"
    return "off"

def build_judge_prompt(case: dict, response: str) -> str:
    return (
        "你是评分员。按以下 rubric 给「模型回答」打分（0-10 分，0.5 步长，10 分=完美）。\n\n"
        f"【rubric】{case.get('judge', '')}\n\n"
        f"【问题】\n{case['prompt']}\n\n"
        f"【模型回答】\n{response}\n\n"
        "只输出一个 JSON 对象，不要任何其他文字：\n"
        '{"score": 数字, "rationale": "评分理由（不少于15字）", "evidence": ["引用回答中的具体片段1", "片段2"]}'
    )

def _parse(text: str):
    m = re.search(r"\{.*\}", text, re.S)
    if not m:
        return None
    try:
        return json.loads(m.group(0))
    except json.JSONDecodeError:
        return None

def score_case(case: dict, response: str, target_model: str):
    """返回 (score, note, meta)。0 分且无理由 → suspect 复核。"""
    if not response or response.startswith("[ERROR") or response.startswith("[REFUSED"):
        return None, "empty/error response", {"verdict": "empty"}

    results = []
    for rnd in (1, 2):
        judge_model = _judge_for(target_model, rnd)
        if _base_name(judge_model) == _base_name(target_model):
            # P2-3：互斥失效 fail loud，绝不静默自评
            return None, f"self-judge blocked: {judge_model} vs {target_model}", {"verdict": "blocked"}
        thinking = _judge_thinking(judge_model, rnd)
        prompt = build_judge_prompt(case, response)
        text, meta = llm.call_deepseek(judge_model, {"type": "disabled"}, prompt, 1024) \
            if not _base_name(judge_model).startswith("MiniMax") else \
            llm.call_minimax(judge_model, {"type": "disabled"}, prompt, 1024)
        parsed = _parse(text)
        results.append({"round": rnd, "judge": judge_model, "parsed": parsed,
                        "raw": text[:200], "meta": meta})
        if parsed and (parsed.get("score") or 0) > 0 and parsed.get("rationale"):
            # 有分数且有理由 → 采信，无需复核
            return float(parsed["score"]), parsed.get("rationale", ""), {
                "verdict": "real", "judge": judge_model,
                "suspect_checked": rnd > 1, "rounds": results}
    # 走到这里：第一轮无有效结果 → 用复核轮
    last = results[-1]["parsed"]
    if last and (last.get("score") or 0) >= 0 and last.get("rationale"):
        return float(last["score"]), last.get("rationale", ""), {
            "verdict": "real", "judge": results[-1]["judge"],
            "suspect_checked": True, "rounds": results}
    return None, f"judge output unparseable: {results[-1]['raw'][:100]}", {
        "verdict": "judge_failed", "rounds": results}
