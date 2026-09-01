"""汇总：bootstrap CI + 分差<0.5 并列 + 报告生成。"""
import json
import random
from collections import defaultdict

from . import config

def _load_scores():
    all_scores = []
    if config.SCORES_DIR.exists():
        for f in config.SCORES_DIR.rglob("*.json"):
            try:
                all_scores.append(json.loads(f.read_text(encoding="utf-8")))
            except Exception:
                continue
    return all_scores

def _bootstrap_ci(values, b=2000, alpha=0.05):
    rng = random.Random(42)
    n = len(values)
    means = []
    for _ in range(b):
        sample = [rng.choice(values) for _ in range(n)]
        means.append(sum(sample) / n)
    means.sort()
    lo = means[int(b * alpha / 2)]
    hi = means[int(b * (1 - alpha / 2))]
    return round(lo, 2), round(hi, 2)

def summarize() -> dict:
    scores = _load_scores()
    cand_scores = defaultdict(list)   # cand -> [score]
    cand_dims = defaultdict(lambda: defaultdict(list))  # cand -> dim -> [score]
    zero_rows, dropped_rows, giveup_rows = [], [], []
    judge_models = set()

    for s in scores:
        if not isinstance(s, dict) or "score" not in s:
            continue
        cid = s.get("cand_id") or s.get("model")
        score = s.get("score")
        dim = s.get("dimension")
        verdict = s.get("verdict", "real")
        if s.get("judge"):
            judge_models.add(s["judge"])
        if score is None:
            (giveup_rows if verdict == "give_up" else dropped_rows).append(s)
            continue
        cand_scores[cid].append(score)
        if dim:
            cand_dims[cid][dim].append(score)
        if score == 0:
            zero_rows.append(s)

    summary = {"candidates": {}, "zero_and_dropped": {
        "real_zeros": [r for r in zero_rows if r.get("verdict") == "real"],
        "refusals": [r for r in zero_rows if r.get("verdict") == "refusal"],
        "technical": dropped_rows, "give_up": giveup_rows},
        "judge_models_used": sorted(judge_models)}
    for cid, vals in cand_scores.items():
        avg = round(sum(vals) / len(vals), 2)
        ci = _bootstrap_ci(vals) if len(vals) >= 3 else (None, None)
        dims = {d: round(sum(v) / len(v), 2) for d, v in cand_dims[cid].items()}
        summary["candidates"][cid] = {
            "avg": avg, "ci95": ci, "n_valid": len(vals), "dims": dims}
    return summary

def _rank(summary: dict):
    rows = sorted(summary["candidates"].items(), key=lambda kv: -kv[1]["avg"])
    ranked = []
    for i, (cid, data) in enumerate(rows):
        tier = data["avg"]
        if i > 0 and abs(tier - rows[i - 1][1]["avg"]) < 0.5:
            tier = rows[i - 1][1]["tie_tier"]  # 与上一名并列
        else:
            tier = i + 1
        data["tie_tier"] = tier
        ranked.append((cid, data, tier))
    return ranked

def write_report(summary: dict, ranked, out: str = None):
    lines = ["# 最小可信基准 v2.1 报告", ""]
    lines.append(f"候选条目数：{len(summary['candidates'])}；judge 模型：{', '.join(summary['judge_models_used'])}")
    lines.append("")
    lines.append("## 总分排名（分差 <0.5 视为并列）")
    lines.append("")
    lines.append("| 并列档 | 候选 | 平均分 | 95% CI | 有效样本 |")
    lines.append("|---|---|---|---|---|")
    for cid, data, tier in ranked:
        lo, hi = data["ci95"] or ("-", "-")
        lines.append(f"| {tier} | {cid} | {data['avg']} | [{lo}, {hi}] | {data['n_valid']} |")
    lines.append("")
    lines.append("## 0 分与剔除项清单")
    z = summary["zero_and_dropped"]
    lines.append(f"- 真实 0 分：{len(z['real_zeros'])} 项")
    for r in z["real_zeros"][:50]:
        lines.append(f"  - {r.get('cand_id', r.get('model'))} / {r.get('case_id')}：{r.get('note', '')}")
    lines.append(f"- 拒答（refusal）：{len(z['refusals'])} 项")
    for r in z["refusals"][:20]:
        lines.append(f"  - {r.get('cand_id', r.get('model'))} / {r.get('case_id')}")
    lines.append(f"- 技术失败（不进统计）：{len(z['technical'])} 项")
    for r in z["technical"][:50]:
        lines.append(f"  - {r.get('cand_id', r.get('model'))} / {r.get('case_id')}：{r.get('note', '')}")
    lines.append(f"- give-up：{len(z['give_up'])} 项")
    for r in z["give_up"][:20]:
        lines.append(f"  - {r.get('cand_id', r.get('model'))} / {r.get('case_id')}：{r.get('note', '')}")
    text = "\n".join(lines)
    target = config.REPORT_FILE if out is None else out
    config.save_json(target, {"text": text})
    (config.BASE / "report.md").write_text(text, encoding="utf-8")
    return text

def main():
    s = summarize()
    ranked = _rank(s)
    text = write_report(s, ranked)
    print(text)
    config.save_json(config.BASE / "scores-summary.json", s)

if __name__ == "__main__":
    main()
