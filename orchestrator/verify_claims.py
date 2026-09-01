"""事实断言检索验证（硬门禁政策）：提取断言清单 + 三分类回写。
初期检索由 host main session web_search 代做（verify_claims 输出待验证清单，接受验证结果回写）。"""
import json
import re
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent  # council/

# 断言提取模式：数字/百分比/金额/专有名词声明
_PATTERNS = [
    (r"(\d[\d,\.]*\s*(?:亿|万|千)?\s*(?:元|美元|美金|美刀|¥|\$|人民币|%|％|个|家|人|次|台|辆|倍))", "quantity"),
    (r"(\d{4}\s*年[^。\n]{0,40}?(?:达|达到|约|为|突破|超过)[^。\n]{0,30}?\d[\d,\.]*(?:亿|万|%|％)?)", "stat_claim"),
]

def extract_claims(text: str) -> list:
    """从 reviewer 输出提取事实断言清单。返回 [{id, kind, snippet, link?, verified?}]。"""
    claims = []
    seen = set()
    # 已有链接的断言（形如 [来源](url) 或 （来源：url））
    linked = re.findall(r"(\[[^\]]+\]\(https?://[^\)]+\)|https?://\S+)", text)
    for pat, kind in _PATTERNS:
        for m in re.finditer(pat, text):
            snippet = m.group(0).strip()
            key = snippet[:50]
            if key in seen:
                continue
            seen.add(key)
            claims.append({"id": f"c{len(claims) + 1}", "kind": kind,
                           "snippet": snippet,
                           "hasLink": any(url in snippet or url in m.string[max(0, m.start() - 80):m.end() + 80]
                                          for url in linked[:20])})
    return claims

def apply_verification(text: str, claims: list, results: list) -> str:
    """把验证结果（confirmed/refuted/unverifiable + link）回写进文本。
    results: [{id, verdict, link, note}]。"""
    out = text
    appended = []
    for r in results:
        c = next((x for x in claims if x["id"] == r["id"]), None)
        if not c:
            continue
        tag = {"confirmed": "✅已核实", "refuted": "❌已证伪", "unverifiable": "⚠️无法核实"}[r["verdict"]]
        line = f"[{tag}] {c['snippet']}"
        if r.get("link"):
            line += f" — {r['link']}"
        if r.get("note"):
            line += f"（{r['note']}）"
        appended.append(line)
    if appended:
        out += "\n\n--- 事实断言验证结果 ---\n" + "\n".join(appended)
    return out

def check_hard_gate(text: str, claims: list, results: list) -> dict:
    """硬门禁检查：未验证的数字/事实断言 >0 即失败（Q6 政策）。"""
    verified_ids = {r["id"] for r in results if r["verdict"] in ("confirmed", "refuted")}
    unverified = [c for c in claims if c["id"] not in verified_ids and not c.get("hasLink")]
    gate_failed = len(unverified) > 0
    return {"gate_failed": gate_failed, "unverified_claims": unverified,
            "total_claims": len(claims)}
