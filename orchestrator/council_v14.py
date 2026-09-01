"""Council v14 主编排：预算预检 → decompose → 收敛循环（交叉执行/交叉验证）→ synthesize。

v15 修复（2026-08-24 council 评审 H1/M3/M4/L1）：
- 反馈回路写回：每轮验证后写 evals/runtime-feedback.jsonl（feedback_ring），
  收敛循环结束后调用 update_capabilities.update() 原子更新能力档案（revision 单调递增）；
- cost_so_far 真实累计（估算 + 实际 usage 计价 + 失败重试都计入），termintor 预算强制终止生效；
- 每轮决策写 termination_audit（max_iter / tie_break / cost 全量审计）；
- 熔断器接线：调用成功/失败调 selector.record_success/record_failure（半开探测结算）；
- 所有退出路径都写 result.json，失败路径退出码非 0（宿主侧 shell.run 检查退出码）；
- 档位化 max_iter（fast 3 / standard 5 / deep 6）+ tie_break_policy=cost_then_latency。

v15.2（2026-08-24 元评审 P0/P1 落地）：
- P0-2：选择 ctx 传 caps_revision；静态不合格候选由 selector 预过滤（事件降噪）；
- P0-3：成本三口径（estBase 对账 / planCny 预算 / actual 真实），token 用量走 token_profiles EWMA，
  每笔调用实时取时（跨峰谷边界不再整段错价）；
- P0-4：mu/epsilon 读 params；maxSubtasks + 动态墙钟预算（wallBudget.*，v15.5）传入 terminator
  （v15.4：墙钟统一 1800s 技术防呆，宿主超时 1920s；maxThinkingRank 已删）；
- P0-5：verifier 强制跨厂商异源；feedback 行带 scoredBy；能力档案回写默认人工审批
  （autoApply=false → 只写 pending diff，--apply 才落盘）；runtime/cost 字段随反馈回填；
- P1-1：decompose 失败回退单子任务计划（不再 exit 1）；
- P1-3：收尾跑 pairwise Elo 横向比较；
- P2-2：汇率 stale level≥2（落后≥3 交易日）直接拒绝开跑。

v15.4（2026-08-24 the maintainer decided成本哲学重构，§14-v15.4）：
- 删预算终止（terminator 成本 forced / 轮前成本护栏 / budget_precheck_over 拒派），
  预检改余额感知报告（只报告不拒派）；
- 汇率停机拒绝删除 → fx_warning 告知主会话（三级 fallback 由 fetch_exchange_rate 承担）；
- 执行者轮换（防全职化）+ 验证者数量动态化 k=clamp(异厂商 baseModel−1,1,3)
  + standard/deep 双路执行互评 + verdict 输出指纹 + rework 优先级 + verifier 广播上下文；
- 收尾统一写 pending diff——autoApply=true 时由插件每日 04:30 体检通过后自动 --apply。
"""
import hashlib
import json
import math
import re
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from orchestrator import (selector, terminator, calibration, budget as budget_mod,
                          stream_llm, verify_claims, config_loader, update_capabilities,
                          params as params_mod, pairwise, token_profiles)
import json_repair  # noqa: E402  v15.5 解析四层防线
import pool  # noqa: E402  v15.5 模型池名单（feedback 污染隔离）

BASE = Path(__file__).resolve().parent.parent  # council/
RUNS = BASE / "runs"
FEEDBACK = BASE / "evals" / "runtime-feedback.jsonl"

# 档位参数外置（评审报告 M 项）：council-params.json 的 tiers.* 覆盖；文件缺失回退默认。
def _tier_params() -> dict:
    return params_mod.load().get("tiers", params_mod.DEFAULTS["tiers"])

TIER_PARAMS = _tier_params()  # 兼容旧引用

DECOMPOSER_MODEL = ("deepseek-v4-flash", "low")   # 默认（3C 动态化 fallback）
SYNTH_DEFAULT = ("deepseek-v4-flash", "low")

# v15.5 3C 角色动态化：角色能力向量（selector 按此从池中选低思考档最优候选）
DECOMPOSE_WV = {"reasoning": 0.6, "instruction_following": 0.4}
SYNTH_WV = {"long_context": 0.4, "instruction_following": 0.35, "chinese": 0.25}
ROLE_ALLOWED_LEVELS = ("off", "low", "minimal")


def _vendor_of_cand(cand: dict) -> str:
    """v15.5-C：候选厂商分组——档案 vendorGroup 优先，缺失时按 provider 规则回退
    （兼容协议对齐前旧档案）。"""
    vg = cand.get("vendorGroup")
    if vg:
        return str(vg)
    p = cand.get("provider") or ""
    if p == "deepseek-official":
        return "deepseek"
    if p == "minimax-cn":
        return "minimax"
    return p or "unknown"


def _pick_role(caps: dict, weight_vector: dict, why: str = ""):
    """3C：按角色向量从池中选最优低思考档候选（stable、身份已知）。
    无候选返回 None（调用方回退默认模型）。"""
    best, best_score = None, -1.0
    for cid, cand in caps.get("models", {}).items():
        if cand.get("thinking") not in ROLE_ALLOWED_LEVELS:
            continue
        if not cand.get("stable", True) or cand.get("identityUnknown"):
            continue
        cap_map = cand.get("capabilities") or {}
        score = sum(w * float((cap_map.get(d) or {}).get("score") or 0)
                    for d, w in weight_vector.items())
        if score > best_score:
            best, best_score = cid, score
    return best

DECOMPOSE_PROMPT = """你是任务分解器。把下面的任务拆成 2-4 个可独立执行的子任务，只输出 JSON（不要其他文字）：

{{
  "subtasks": [
    {{"id": "s1", "title": "...", "description": "...",
      "weightVector": {{"reasoning": 0.2, "code": 0.1, "chinese": 0.2, "research": 0.2,
                        "instruction_following": 0.1, "long_context": 0.1,
                        "tool_use": 0.0, "creativity": 0.05, "safety": 0.05}},
      "dependencies": []}}
  ],
  "synthesisNotes": "..."
}}

规则：
- weightVector 各维度之和必须为 1；按子任务真实需求分配（代码任务 code 高、研究任务 research 高）。
- dependencies 列出依赖的前置子任务 id（可空）。
- 每个子任务描述必须具体可执行。

任务：
{task}"""

# v15.5c（元评审 2026-08-26 + 自评测 2026-08-26_20-59-21）：时效性核查规则——exec/verifier 共用。
# 目标：杜绝「引用过时信息」——时间锚、证据来源、审计日志区分、失败证据深挖、快照为准。
# v15.5c 修订：明确本执行环境【无工具调用能力】——原规则 2 要求「当场 grep/read」，
# 实测无工具模型会输出未执行的 shell 命令文本后终止（自评测 s2 零输出硬门禁失败），
# 改为「引用宿主快照数据或显式标注未现场核实」。
FRESHNESS_RULES = """【时效性核查规则（v15.5c，必须遵守）】
1. 时间锚：任何关于「当前状态」的断言（revision/分数/失败/配置）必须标注「截至 <时间戳>」，时间戳取宿主快照锚点 / 文件 mtime / 事件 ts / generatedAt；无时间锚的「当前是 X」视为无效断言。
2. 现状与证据来源：本执行环境【没有工具调用能力】，无法真的读取或搜索文件——禁止输出 shell 命令、grep/read 命令或任何「计划执行的命令」，也禁止声称已执行工具。需要引用状态/数字时，一律引用任务文本「宿主权威状态快照」中的数据（标注「快照为据，截至 <快照时间>」）；快照未覆盖而你又无法确认的断言，必须显式标注「未现场核实（推测）」并降级表述。
3. 审计日志区分：failed_runs.log 等历史审计日志，引用失败必须带日期，并检查 resolution/resolvedAt 字段——带 resolution 的失败是已处置历史，不得当作当前故障；「近 N 小时/天内」必须按 ts 实际过滤。
4. 失败证据深挖：任务文本/快照中如给出 verdict-raw 的 meta 字段（http_status / timeout_kind / text 长度）或「最近 run verifier 分类统计」，以此为准区分「无输出（429 限流/超时）」与「有输出但解析失败」——两者根因完全不同；不要凭空断言失败原因。
5. 快照为准：任务文本附带的「宿主权威状态快照」是最新基准；与快照矛盾的旧数字以快照为准，并在报告中显式标注差异。"""

VERIFY_PROMPT = """你是验证员。检查下面的「子任务输出」是否回答了子任务要求，并核对「事实断言验证结果」。

{FRESHNESS_RULES}

子任务：{subtask_title}
要求：{subtask_description}

子任务输出：
{output}

事实断言验证结果：
{claims_verification}

其他子任务的产出摘要（用于发现子任务间的矛盾，没有则忽略）：
{context}

只输出 JSON（不要其他文字）：
{{
  "dimScores": {{"factual": 0-5, "logic": 0-5, "completeness": 0-5, "actionability": 0-5}},
  "overallScore": 0-10,
  "hardGateFailed": true/false,
  "hardGateReasons": ["..."],
  "reworkList": [{{"target": "哪条结论/段落", "issue": "缺证据/存疑/矛盾", "expected": "期望产出", "priority": "高/中/低"}}],
  "rationale": "..."
}}

硬门禁（任一触发 hardGateFailed=true）：
- 断言验证结果中标记「已证伪(❌)」的断言
- 关键结论无证据支撑
- 与其他子任务产出存在未裁决的关键矛盾
- reworkList 为空数组表示无需返工；priority 按问题严重度标注（证据缺失/事实错误=高，表述不清=中，优化建议=低）。
注意：若 claims_verification 显示「检索验证待接入」，不要因此判 hardGateFailed，只针对输出内部质量打分。"""

SYNTH_PROMPT = """你是综合器。综合所有子任务输出、验证结果与返工历史，产出最终结论。

原始任务：{task}

子任务输出与验证：
{combined}

{FRESHNESS_RULES}

只输出最终报告（Markdown），结构：
## 结论
## 关键发现
## 共识与分歧
## 风险
## 建议与下一步
## 置信度（0-1 + 一句话理由）
## 证据清单（必须附在报告末尾）
- 报告生成时刻：<当前时间>
- 每个关键数字 → 来源文件 + 事件 ts / mtime / generatedAt（引用时带「截至」时间锚）
- 与任务文本快照不一致的数字 → 显式列出差异与依据

状态语义说明（v15.5）：收敛质量线 θ=9.5 为理论满分天花板，多数 run 以「增长枯竭」（early_stop，近 3 轮 S_r 极差 < 0.2）收尾属预期常态，不是异常；请在报告中如实呈现本轮状态与 S_r 轨迹，不要为未达 9.5 而道歉或找补。"""

def _extract_json(text: str):
    """v15.5：公共 lenient 解析（fence/裸换行/截断/未转义引号四修复）。"""
    return json_repair.parse(text)

def _log(run_dir: Path, name: str, line: str):
    with (run_dir / name).open("a", encoding="utf-8") as f:
        f.write(json.dumps(line, ensure_ascii=False) + "\n")

def _append_feedback(row: dict):
    """feedback_ring 追加（H1）：结构含 case_id / model@thinking / score / latency_ms / cost_usd / ts。"""
    FEEDBACK.parent.mkdir(parents=True, exist_ok=True)
    with FEEDBACK.open("a", encoding="utf-8") as f:
        f.write(json.dumps(row, ensure_ascii=False) + "\n")

def _provider_of(model: str) -> str:
    if model.startswith("MiniMax"):
        return "minimax-cn"
    return "deepseek-official"

def _cost_pair(model: str, thinking: str, est_in: int, est_out: int,
               meta: dict = None, balance=None, role: str = None):
    """返回 (estBaseCny, planCny, actualCny|None)。P0-3 对账口径：
    estBase 与 actual 同构（真实单价×token、含缓存命中、无 thinking/quota 规划系数）
    → cost_calibrate 的 drift 可比；planCny 含规划系数，供预算防呆/选择。
    v15.3：role 传给校准系数（per-(model,role) 级），cost_calibrate 每日回填闭环。
    每笔调用实时取时（此前 run 开始冻结 now，跨峰谷边界整段错价）。"""
    now = config_loader.now_shanghai()
    provider = _provider_of(model)
    info = selector.effective_cost_cny(provider, model, thinking, est_in, est_out,
                                       now=now, balance=balance, calib_role=role)
    est_base = info.get("base_cost_cny") or 0.0
    plan = info.get("cost_cny") or 0.0
    if not (meta and meta.get("usage")):
        return est_base, plan, None
    pricing = selector.load_pricing().get("providers", {}).get(provider, {})
    mp = (pricing.get("models") or {}).get(model) or {}
    tf = selector.time_factor(provider, now)
    def pick(d):
        return (d.get("peak") if tf > 0.9 else d.get("offpeak")) or 0.0
    rate_in = pick(mp.get("inputCnyPerMTok") or {})
    rate_out = pick(mp.get("outputCnyPerMTok") or {})
    rate_cache = pick(mp.get("cacheInputCnyPerMTok") or {}) or rate_in
    u = meta["usage"]
    cache_tok = int(u.get("cacheHitTokens") or 0)
    in_tok = max(int(u.get("promptTokens") or 0) - cache_tok, 0)
    out_tok = int(u.get("completionTokens") or 0)
    actual = (in_tok * rate_in + out_tok * rate_out + cache_tok * rate_cache) / 1_000_000
    return est_base, plan, actual

def run_council(task: str, tier: str = "standard", mode: str = "report",
                dry: bool = False, facts: str = None) -> dict:
    params = _tier_params().get(tier) or _tier_params()["standard"]
    all_params = params_mod.load()
    sel_params = all_params.get("selection", {})
    ts = config_loader.now_shanghai().strftime("%Y-%m-%d_%H-%M-%S")
    run_dir = RUNS / ts
    run_dir.mkdir(parents=True, exist_ok=True)
    # v15.5c（元评审 2026-08-26）：宿主权威状态快照（--facts）附加到任务文本——
    # 给评审员「截至 now」的时间锚基准，防人写任务文本注入过时前提（如 revision/failed_runs 无日期）。
    if facts:
        try:
            snap = Path(facts).read_text(encoding="utf-8").strip()
            if snap:
                task = task + "\n\n" + snap
        except Exception as e:
            _log_raw = (run_dir / "rounds.jsonl").open("a", encoding="utf-8")
            _log_raw.write(json.dumps({"event": "facts_load_failed", "error": str(e)[:200]}) + "\n")
            _log_raw.close()
        finally:
            try:  # 快照中间文件读完即删（内容已并入 task.md，不留垃圾）
                Path(facts).unlink()
            except OSError:
                pass
    (run_dir / "task.md").write_text(task, encoding="utf-8")
    run_id = ts

    def _finish(result: dict, exit_code: int = 0) -> dict:
        """所有退出路径统一写 result.json（宿主侧按 mtime 取最新 run，缺文件会静默回退旧 run）。"""
        (run_dir / "result.json").write_text(
            json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
        result["_exitCode"] = exit_code
        return result

    balance = None
    try:
        from orchestrator import query_balance
        balance = query_balance.query().get("ok", {})
    except Exception:
        pass
    now = config_loader.now_shanghai()

    # ---- 0. 汇率状态检查（v15.4：停机拒绝已删除——stale 只降级成本置信度 + fx_warning）----
    # 出结果是终极使命；单次 run 消耗仅几分钱，汇率波动影响微乎其微（the maintainer decided）。
    fx = selector.fx_status()
    fx_warning = None
    if fx.get("level", 0) >= 1:
        fx_warning = {"level": fx.get("level"), "tradingDaysBehind": fx.get("tradingDaysBehind"),
                      "usdToCny": fx.get("usdToCny"), "staleReasons": fx.get("staleReasons", []),
                      "hint": "汇率陈旧：本次成本估算精度降级（不影响运行）。若持续陈旧请检查 fetch_exchange_rate.py（三级 fallback 链）"}
    if fx.get("level", 0) >= 2:
        fx_warning["hint"] = ("汇率 API 主源/备用源皆不可用（已用最近一次成功汇率），"
                              "建议修复汇率更新任务")

    # ---- 1. decompose ----
    # v15.5b（元评审 F15）：run 级警告收集（解析全败/验证退化/替补失败），result.json 可见
    _warnings = []
    # v15.3：fast 档任务长度护栏——任务过大是 P95 超时的结构性主因，decompose 前直接提示降档
    max_task_tokens = params.get("maxTaskTokens")
    if max_task_tokens and selector.estimate_tokens(task) > int(max_task_tokens):
        return _finish({"status": "task_too_large_for_tier",
                        "taskTokens": selector.estimate_tokens(task),
                        "limit": int(max_task_tokens), "tier": tier, "mode": mode,
                        "run_dir": str(run_dir),
                        "hint": f"任务长度超过 {tier} 档上限 {max_task_tokens} token，请精简任务或改用 standard/deep 档"},
                       exit_code=0)
    # v15.5 3C：decompose 角色动态化——按能力向量从池中选低思考档候选，回退默认
    caps = selector.load_capabilities()
    caps_revision = caps.get("revision")
    role_cid = _pick_role(caps, DECOMPOSE_WV, "decompose")
    decomposer = tuple(role_cid.rsplit("__", 1)) if role_cid else DECOMPOSER_MODEL
    _log(run_dir, "decisions.jsonl",
         {"event": "role_assign", "role": "decompose",
          "model": decomposer[0], "thinking": decomposer[1],
          "weightVector": DECOMPOSE_WV,
          "reason": f"3C 角色动态选择（候选 {role_cid or '无→默认'}）"})
    plan = None
    dmeta = {}
    for attempt in range(2):
        text, dmeta = stream_llm.call_stream(decomposer[0], decomposer[1],
                                             DECOMPOSE_PROMPT.format(task=task),
                                             config_loader.max_tokens_for_model(decomposer[0]))
        # v15.1：decompose 尝试落盘（失败时可诊断原始输出，不再黑盒）
        _log(run_dir, "decompose-attempts.jsonl",
             {"attempt": attempt + 1, "textTail": text[-400:], "len": len(text),
              "timeoutKind": dmeta.get("timeout_kind"),
              "finishReason": dmeta.get("finish_reason")})
        du = dmeta.get("usage") or {}
        token_profiles.record(decomposer[0], "decompose",
                              du.get("promptTokens"), du.get("completionTokens"),
                              du.get("cacheHitTokens") or 0)
        plan = _extract_json(text)
        if plan and plan.get("subtasks"):
            break
    if not plan:
        # P1-1：decompose 兜底——畸形任务/分解器输出散文时不 exit 1，回退单子任务计划
        _log(run_dir, "rounds.jsonl",
             {"event": "decompose_fallback", "reason": "两次 JSON 解析失败，回退单子任务计划"})
        subtasks = [{"id": "s1", "title": task[:60], "description": task,
                     "weightVector": {"reasoning": 0.25, "chinese": 0.15, "research": 0.15,
                                      "instruction_following": 0.15, "long_context": 0.1,
                                      "code": 0.05, "tool_use": 0.05, "creativity": 0.05,
                                      "safety": 0.05},
                     "dependencies": []}]
    else:
        subtasks = plan["subtasks"]
    # P0-4：档位子任务数上限（fast 3 / standard 4 / deep 4）
    max_sub = int(params.get("maxSubtasks") or 99)
    if len(subtasks) > max_sub:
        _log(run_dir, "rounds.jsonl", {"event": "subtask_truncated",
             "from": len(subtasks), "to": max_sub, "tier": tier})
        subtasks = subtasks[:max_sub]

    # ---- 2. 余额感知预检（v15.4：只报告不拒派——预算只做选择参考，不做用量控制） ----
    for sub in subtasks:
        sub["inputChars"] = str(sub.get("title", "")) + str(sub.get("description", "")) + task
    pre = budget_mod.precheck(subtasks, 0.0,
                              default_cand=f"{decomposer[0]}__{decomposer[1]}",
                              now=now, balance=balance, safety_factor=1.5,
                              max_rounds=None)
    _log(run_dir, "budget.jsonl", {"event": "precheck", **pre})
    if dry:
        return _finish({"status": "ok_dry", "subtasks": len(subtasks), "precheck": pre,
                        "run_dir": str(run_dir), "tier": tier, "mode": mode}, exit_code=0)

    # ---- 3. 收敛循环 ----
    # （caps/caps_revision 已在 decompose 前加载，3C 角色选择共用）
    st = terminator.RoundState()
    cost_log = []           # 累计每笔调用规划成本（含失败重试，v15.4 起仅审计用，不参与终止）
    outputs = {}            # subtask_id -> text（每轮验证合并后的正式产出）
    verdicts = {}           # subtask_id -> 合并后 verdict json
    subtasks_map = {s["id"]: s.get("weightVector", {}) for s in subtasks}
    wall_start = time.time()
    cache_hit_rate = token_profiles.cache_hit_rate()
    ver_params = all_params.get("verification", {})
    max_verifiers = int(ver_params.get("maxVerifiers", 3))
    min_verifiers = int(ver_params.get("minVerifiers", 1))
    dual_exec = tier in (ver_params.get("dualExecTiers") or ["standard", "deep"])
    # v15.5 动态墙钟预算：min(maxS, baseS + 每子任务×(执行位数+验证者数)×perSlotS)，
    # 规模自适应；maxS 默认 1680 = 宿主 1920s − 240s 收尾余量（synthesize+落盘）。
    wb = params_mod.get(all_params, "wallBudget", {}) or {}
    wb_base = float(wb.get("baseS", 600))
    wb_per = float(wb.get("perSlotS", 60))
    wb_max = float(wb.get("maxS", 1680))
    slots = len(subtasks) * ((2 if dual_exec else 1) + min(max_verifiers, 3))
    wall_budget_s = min(wb_max, wb_base + slots * wb_per)

    def _decompose_cost():
        est, plan, _act = _cost_pair(decomposer[0], decomposer[1],
                                     selector.estimate_tokens(task) + 400, 800,
                                     meta=dmeta, balance=balance, role="decompose")
        return plan

    cost_log.append(_decompose_cost())

    # v15.5-C（3A）：可用厂商集合（vendorGroup）——执行者轮换与验证者的配额基数。
    # 修复 v15.4 E-17 漏洞：此前以 baseModel 计数，v4-pro/v4-flash 同厂商被当异厂商互评。
    avail_vendors = []
    for _cid, _cand in caps.get("models", {}).items():
        if _cand.get("identityUnknown") or not _cand.get("stable", True):
            continue
        _v = _vendor_of_cand(_cand)
        if _v and _v not in avail_vendors:
            avail_vendors.append(_v)

    # v15.5 子任务覆盖修复（v15.5b 修正：截断与执行配额的公式必须一致）——
    # 总执行位 = 子任务数 × n_exec，每厂商上限 cap = ceil(总执行位 / 厂商数)。
    # 截断到 feasible = (厂商数 × cap) // n_exec（防 no_candidate 结构性轮空）。
    # 元评审 21-40-15 实测：旧公式截断用截断前 subtasks 数、执行用截断后数，
    # standard 双路下 s3 必然轮空（s1 吃满 minimax+deepseek 配额后 s2 只剩 1 位）。
    _n_exec_pre = 2 if dual_exec else 1
    _cap_pre = math.ceil(len(subtasks) * _n_exec_pre / max(1, len(avail_vendors)))
    _max_feasible = max(1, (len(avail_vendors) * _cap_pre) // _n_exec_pre)
    if len(subtasks) > _max_feasible:
        _log(run_dir, "rounds.jsonl",
             {"event": "subtask_capacity_truncate", "from": len(subtasks),
              "to": _max_feasible, "reason": "子任务×执行位数超过厂商配额容量（防轮空）"})
        subtasks = subtasks[:_max_feasible]
        subtasks_map = {s["id"]: s.get("weightVector", {}) for s in subtasks}

    while True:
        r = st.round_no
        _log(run_dir, "rounds.jsonl", {"event": "round_start", "round": r})
        # ---- assigning：执行者轮换（防全职化）+ 动态验证者（v15.5-C 厂商级互斥）----
        used_providers = set()   # 每轮重置
        assignment = {}
        # v15.5b：cap 与截断同式（总执行位 / 厂商数），保证每轮全部子任务都有可派执行位
        cap_per_vendor = math.ceil(len(subtasks) * (2 if dual_exec else 1) / max(1, len(avail_vendors)))
        vendor_use = {v: 0 for v in avail_vendors}
        for i, sub in enumerate(subtasks):
            tv = sub.get("weightVector", {})
            ctx = {"lambda_": params["lambda_"],
                   "mu": float(params_mod.get(all_params, "selection.mu", 0.001)),
                   "used_providers": used_providers,
                   "banned_models": set(), "balance": balance,
                   "epsilon": float(params_mod.get(all_params, "selection.epsilon", 0.05)),
                   "est_input_tokens": selector.estimate_tokens(task) + 400,
                   "est_output_tokens": 800, "now": now, "run_id": run_id,
                   "caps_revision": caps_revision,
                   "cache_hit_rate": cache_hit_rate}
            ranked = selector.select(tv, ctx, caps)
            # 执行者轮换——按评分顺序找厂商轮内配额未满的候选（selector 定能力，轮换定参与）
            # v15.5b K1：厂商均衡排序——轮内使用最少的厂商优先（同配额内），
            # 使各子任务执行组合分散（K1 试跑实测：3 子任务组合相同 → verifier 全押 ox 单点）
            n_exec = 2 if dual_exec else 1
            execs = []
            ranked_balanced = sorted(
                [rk for rk in ranked if rk[2] is not None],
                key=lambda rk: (vendor_use.get(_vendor_of_cand(rk[1]), 0), -float(rk[2])))
            for rk in ranked_balanced:
                vendor = _vendor_of_cand(rk[1])
                if vendor in vendor_use and vendor_use[vendor] >= cap_per_vendor:
                    continue
                if vendor in {e["vendor"] for e in execs}:
                    continue  # 双路 exec 之间厂商互斥（v15.5-C）
                execs.append({"cid": rk[0], "cand": rk[1], "vendor": vendor, "cost": rk[3]})
                vendor_use[vendor] = vendor_use.get(vendor, 0) + 1
                if len(execs) >= n_exec:
                    break
            if not execs:
                _log(run_dir, "rounds.jsonl", {"event": "no_candidate", "round": r, "subtask": sub["id"]})
                continue
            # v15.5c（元评审 2026-08-26 K1/D3）：dual_exec 降级守卫——
            # 3 厂商下双执行者占 2 厂后验证厂商只剩 1 家（数学必然），若该唯一验证厂商
            # 不可用（实测 stealth 429 限流致 s1/s2 整轮无有效验证），则该子任务验证全废。
            # 可用验证厂商 < 2 时降级为单执行者，保证 ≥2 家验证厂商（交叉验证恢复）。
            exec_vendors = {e["vendor"] for e in execs}
            if dual_exec and len(execs) >= 2 and len(avail_vendors) - len(exec_vendors) < 2:
                _log(run_dir, "rounds.jsonl",
                     {"event": "dual_exec_degraded", "round": r, "subtask": sub["id"],
                      "reason": f"可用验证厂商 {len(avail_vendors) - len(exec_vendors)} 家 < 2，降级为单执行者以恢复交叉验证",
                      "droppedExec": execs[-1]["cid"]})
                _warnings.append(f"第{r}轮 s{sub['id']} dual_exec 降级为单执行者（验证厂商不足 2 家，丢弃 {execs[-1]['cid']}）")
                execs = execs[:1]
                exec_vendors = {e["vendor"] for e in execs}
            # v15.5-C：k = clamp(可用厂商数 − 1, min, max)；verifier 与执行者 provider 全量互斥
            # （hetero 原则）+ verifier 之间厂商互斥（修复 v4-pro/v4-flash 同厂商互评）
            exec_providers = {e["cand"].get("provider") for e in execs}
            hetero_banned = {c.get("baseModel") for c in caps.get("models", {}).values()
                             if c.get("provider") in exec_providers}
            avail_v_vendors = [v for v in avail_vendors if v not in exec_vendors]
            k = max(min_verifiers, min(max_verifiers, len(avail_v_vendors)))
            # v15.5b（元评审 F14）：k 退化定义——可用验证厂商 < 2 时显式告警
            # （2 厂商 → k=1 单验证者；1 厂商 → 无异厂商可验证，本轮该子任务验证缺失，
            # 靠无输出硬门禁/低置信度兜底，不再静默）
            if len(avail_v_vendors) < 2:
                _log(run_dir, "rounds.jsonl",
                     {"event": "verifier_degraded", "round": r, "subtask": sub["id"],
                      "availVerifierVendors": len(avail_v_vendors),
                      "k": k, "warn": "可用验证厂商不足 2 家，交叉验证退化/缺失"})
                _warnings.append(f"第{r}轮 s{sub['id']} 验证厂商退化（{len(avail_v_vendors)} 家）")
            verifier_ranked = selector.select(tv, {**ctx, "banned_models": hetero_banned}, caps)
            verifiers = []
            used_vendors_v = set()
            for rk in verifier_ranked:
                if rk[2] is None:
                    continue
                vv = _vendor_of_cand(rk[1])
                if vv in exec_vendors or vv in used_vendors_v:
                    continue
                verifiers.append({"cid": rk[0], "cand": rk[1], "vendor": vv, "cost": rk[3]})
                used_vendors_v.add(vv)
                if len(verifiers) >= k:
                    break
            assignment[sub["id"]] = {"execs": execs, "verifiers": verifiers}
            for e in execs:
                used_providers.add(e["cand"].get("provider"))
            for v in verifiers:
                used_providers.add(v["cand"].get("provider"))
        _log(run_dir, "decisions.jsonl",
             {"event": "assign", "round": r,
              "assignment": {s: {"execs": [e["cid"] for e in a["execs"]],
                                 "verifiers": [v["cid"] for v in a["verifiers"]]}
                             for s, a in assignment.items()},
              "reason": "selector v15.5-C (vendorQuota capPerVendor=%d, verifierK=%d, dualExec=%s, availVendors=%s)" %
                        (cap_per_vendor, k, dual_exec, avail_vendors)})

        # ---- v15.4 轮前墙钟预检（技术防呆；v15.3 成本护栏已删——预算不做终止）----
        round_lat_ms = 0.0
        lat_known = False
        for sub in subtasks:
            asg = assignment.get(sub["id"]) or {}
            for e in asg.get("execs", []):
                lat = (e["cand"].get("runtime") or {}).get("latencyP50Ms") or e["cand"].get("latencyP50Ms")
                if isinstance(lat, (int, float)) and lat > 0:
                    round_lat_ms += float(lat)
                    lat_known = True
        est_this_round_s = 0.0
        if lat_known and wall_budget_s > 0:
            est_this_round_s = round_lat_ms / 1000.0 * 1.5  # 1.5× 覆盖超 P50 尾延迟与 synthesize
            if (time.time() - wall_start) + est_this_round_s > wall_budget_s:
                action = "forced"
                reason = (f"v15.4 轮前墙钟预检：预估本轮耗时 {est_this_round_s:.0f}s "
                          f"将突破墙钟 {wall_budget_s:.0f}s（技术防呆，防宿主超时丢结果）")
                _log(run_dir, "rounds.jsonl",
                     {"event": "termination_audit", "action": action, "reason": reason,
                      "round": r, "wallBudgetS": wall_budget_s,
                      "wallElapsedS": round(time.time() - wall_start, 1),
                      "sHistory": st.s_history, "callCount": len(cost_log),
                      "roundLatEstMs": round(round_lat_ms, 0)})
                break

        # ---- reviewing：流式 + 事实断言清单（每 exec 一路，多路并发） ----
        def _review_one(job):
            sid, exec_idx, e = job
            exec_cid = e["cid"]
            model, think = _parse(exec_cid)
            exec_prompt = (
                f"你是执行员。完成以下子任务。\n\n子任务：{subtask_by_id[sid]['title']}\n描述：{subtask_by_id[sid]['description']}\n"
                f"依赖的上轮输出（如有）：{_gather_deps(subtask_by_id[sid], outputs)}\n\n"
                f"原始任务背景：{task}\n\n"
                f"{FRESHNESS_RULES}\n\n"
                f"输出要求：结论必须附证据；每个数字/事实断言在文末单列「事实断言清单」（每条：内容+你声称的来源）。"
                f"控制 800 字以内。")
            est_in_heur = selector.estimate_tokens(exec_prompt) + 400
            est_in, est_out = token_profiles.est_for(model, "exec", est_in_heur, 800)
            try:
                text, meta = stream_llm.call_stream(model, think, exec_prompt,
                                                    config_loader.max_tokens_for_model(model))
                failed = bool(meta.get("timeout_kind")) or meta.get("finish_reason") == "error" or not text.strip()
                if failed:
                    selector.record_failure(model)   # M4：熔断器接线（失败结算）
                else:
                    selector.record_success(model)
                u = meta.get("usage") or {}
                token_profiles.record(model, "exec", u.get("promptTokens"),
                                      u.get("completionTokens"), u.get("cacheHitTokens") or 0)
                cost, plan, actual = _cost_pair(model, think, est_in, est_out, meta=meta, balance=balance, role="exec")
            except Exception as e2:
                selector.record_failure(model)
                text, meta = "", {"error": str(e2)[:200], "timeout_kind": "exception"}
                cost, plan, actual = _cost_pair(model, think, est_in, est_out, meta=None, balance=balance, role="exec")
            # v15.4 E-22：输出指纹（orchestrator 侧绑定——合成阶段校验 verdict 对应版本）
            out_hash = hashlib.sha256(text.encode("utf-8")).hexdigest()[:16] if text else ""
            claims = verify_claims.extract_claims(text)
            (run_dir / f"claims-r{r}-{sid}-e{exec_idx}.json").write_text(
                json.dumps({"subtask": sid, "execIdx": exec_idx, "claims": claims,
                            "retrieval": "pending", "outputHash": out_hash},
                           ensure_ascii=False, indent=2),
                encoding="utf-8")
            _log(run_dir, "cost.jsonl",
                 {"round": r, "subtask": sid, "execIdx": exec_idx, "role": "exec", "model": model, "thinking": think,
                  "timeoutKind": meta.get("timeout_kind"), "elapsedS": meta.get("elapsed_s"),
                  "usage": meta.get("usage"), "estCostCny": round(cost, 6),
                  "planCostCny": round(plan, 6),
                  "actualCostCny": round(actual, 6) if actual is not None else None,
                  "costUsd": round(plan / (selector.load_fx_rate().get("usdToCny") or 1), 8)})
            return (sid, exec_idx, text, meta, cost, plan, actual, out_hash)

        subtask_by_id = {s["id"]: s for s in subtasks}
        from concurrent.futures import ThreadPoolExecutor as _TPE
        exec_results = {}
        outputs_raw = {}   # (sid, exec_idx) -> text（验证前多路并存）
        review_jobs = [(sid, ei, e) for sid, asg in assignment.items()
                       for ei, e in enumerate(asg.get("execs", []))]
        workers = min(8, max(1, len(review_jobs)))
        with _TPE(max_workers=workers) as ex:
            for result in ex.map(_review_one, review_jobs):
                if result:
                    sid, exec_idx, text, meta, cost, plan, actual, out_hash = result
                    outputs_raw[(sid, exec_idx)] = {"text": text, "hash": out_hash,
                                                    "model": _parse(assignment[sid]["execs"][exec_idx]["cid"])[0]}
                    exec_results[(sid, exec_idx)] = {"meta": meta, "cost": cost,
                                                     "plan": plan, "actual": actual}
                    cost_log.append(plan)

        # ---- verifying：多路输出 × 动态数量验证者（v15.4 E-17/20/22/24） ----
        def _context_for(sid):
            # E-24 广播语义：其他子任务产出摘要（供交叉矛盾检查）
            parts = []
            for other_sid, other_asg in assignment.items():
                if other_sid == sid:
                    continue
                for (o_sid, o_idx), rec in outputs_raw.items():
                    if o_sid == other_sid and rec.get("text"):
                        parts.append(f"[{o_sid}]（{rec.get('model')}）：{rec['text'][:200]}")
            return ("\n".join(parts) or "（无）")

        def _verify_one(job):
            sid, exec_idx, v_idx = job
            asg = assignment.get(sid) or {}
            verifiers = asg.get("verifiers", [])
            if v_idx >= len(verifiers):
                return None
            v = verifiers[v_idx]
            rec = outputs_raw.get((sid, exec_idx))
            if not rec or not rec.get("text"):
                return None
            out_text = rec["text"]
            out_hash = rec["hash"]
            vmodel, vthink = _parse(v["cid"])
            claims_file = run_dir / f"claims-r{r}-{sid}-e{exec_idx}.json"
            claims_data = json.loads(claims_file.read_text(encoding="utf-8")) if claims_file.exists() else {"claims": [], "retrieval": "pending"}
            est_in_heur = selector.estimate_tokens(out_text) + 800
            est_in, est_out = token_profiles.est_for(vmodel, "verifier", est_in_heur, 500)
            try:
                verdict, gmeta = stream_llm.call_stream(
                    vmodel, vthink,
                    VERIFY_PROMPT.format(FRESHNESS_RULES=FRESHNESS_RULES,
                                         subtask_title=subtask_by_id[sid]["title"],
                                         subtask_description=subtask_by_id[sid]["description"],
                                         output=out_text,
                                         claims_verification=json.dumps(claims_data, ensure_ascii=False),
                                         context=_context_for(sid)),
                    config_loader.max_tokens_for_model(vmodel))
                vfailed = bool(gmeta.get("timeout_kind")) or gmeta.get("finish_reason") == "error"
                if vfailed:
                    selector.record_failure(vmodel)
                else:
                    selector.record_success(vmodel)
                vu = gmeta.get("usage") or {}
                token_profiles.record(vmodel, "verifier", vu.get("promptTokens"),
                                      vu.get("completionTokens"), vu.get("cacheHitTokens") or 0)
                vcost, vplan, vactual = _cost_pair(vmodel, vthink, est_in, est_out, meta=gmeta, balance=balance, role="verifier")
            except Exception as e3:
                selector.record_failure(vmodel)
                verdict, gmeta = "", {"error": str(e3)[:200]}
                vcost, vplan, vactual = _cost_pair(vmodel, vthink, est_in, est_out, meta=None, balance=balance, role="verifier")
            # ---- v15.5 解析四层防线：①lenient 解析 ②关键字段降级抠取 ③诊断重试一次 ④（外层）替补补位 ----
            vj = _extract_json(verdict)
            parse_stage = "ok"
            if vj is None:
                vj = json_repair.extract_verdict_fields(verdict)
                parse_stage = "partial-fields" if vj is not None else "failed"
            if vj is None and not (gmeta or {}).get("timeout_kind"):
                # 防线③：诊断重试一次（把解析失败反馈给模型，只重试一次）
                retry_prompt = (VERIFY_PROMPT.format(
                    FRESHNESS_RULES=FRESHNESS_RULES,
                    subtask_title=subtask_by_id[sid]["title"],
                    subtask_description=subtask_by_id[sid]["description"],
                    output=out_text,
                    claims_verification=json.dumps(claims_data, ensure_ascii=False),
                    context=_context_for(sid))
                    + "\n\n⚠ 你上一次的输出不是合法 JSON（系统无法解析）。"
                      "请严格只输出一个 JSON 对象（不要 markdown 代码块、不要额外文字）。"
                      f"你上次输出的结尾：\n{(verdict or '')[-500:]}")
                try:
                    verdict2, gmeta2 = stream_llm.call_stream(
                        vmodel, vthink, retry_prompt,
                        config_loader.max_tokens_for_model(vmodel))
                    vu2 = (gmeta2 or {}).get("usage") or {}
                    token_profiles.record(vmodel, "verifier", vu2.get("promptTokens"),
                                          vu2.get("completionTokens"), vu2.get("cacheHitTokens") or 0)
                    rvcost, rvplan, rvactual = _cost_pair(vmodel, vthink, est_in, est_out,
                                                          meta=gmeta2, balance=balance, role="verifier")
                    vplan += rvplan
                    _log(run_dir, "cost.jsonl",
                         {"round": r, "subtask": sid, "execIdx": exec_idx, "verifierIdx": v_idx,
                          "role": "verifier-retry", "model": vmodel, "thinking": vthink,
                          "usage": vu2, "planCostCny": round(rvplan, 6),
                          "actualCostCny": round(rvactual, 6) if rvactual is not None else None})
                    vj = _extract_json(verdict2) or json_repair.extract_verdict_fields(verdict2)
                    parse_stage = "retried-ok" if vj is not None else "retried-failed"
                    verdict = verdict2
                except Exception:
                    parse_stage = "retry-error"
            # verdict-raw 落盘改存全文（失败可复诊）+ 解析阶段标记
            (run_dir / f"verdict-raw-r{r}-{sid}-e{exec_idx}-v{v_idx}.json").write_text(
                json.dumps({"subtask": sid, "execIdx": exec_idx, "verifier": vmodel,
                            "thinking": vthink, "runId": run_id,
                            "ts": config_loader.now_shanghai().isoformat(),
                            "parsed": vj is not None,
                            "parseStage": parse_stage,
                            "outputHash": out_hash, "text": (verdict or "")[:4000],
                            "meta": {k: (str(val)[:200] if not isinstance(val, dict) else val)
                                     for k, val in (gmeta or {}).items()}},
                           ensure_ascii=False, indent=2),
                encoding="utf-8")
            _log(run_dir, "cost.jsonl",
                 {"round": r, "subtask": sid, "execIdx": exec_idx, "verifierIdx": v_idx,
                  "role": "verifier", "model": vmodel, "thinking": vthink,
                  "timeoutKind": gmeta.get("timeout_kind"), "elapsedS": gmeta.get("elapsed_s"),
                  "usage": gmeta.get("usage"), "estCostCny": round(vcost, 6),
                  "planCostCny": round(vplan, 6),
                  "actualCostCny": round(vactual, 6) if vactual is not None else None})
            if not vj:
                _log(run_dir, "rounds.jsonl",
                     {"event": "verifier_parse_fail", "round": r, "subtask": sid,
                      "execIdx": exec_idx, "verifier": vmodel, "parseStage": parse_stage})
                _warnings.append(f"第{r}轮 s{sid} 验证者 {vmodel} 解析全败（{parse_stage}）")
                return None
            return (sid, exec_idx, vmodel, vj, vplan, out_hash)

        def _merge_verdicts(vjs):
            """E-18：多 verdict 合成——hardGateFailed 取并集、overallScore 取均值、reworkList 并集去重。"""
            vjs = [v for v in vjs if v]
            if not vjs:
                return None
            scores = [float(v.get("overallScore") or 0) for v in vjs]
            merged = {
                "overallScore": round(sum(scores) / len(scores), 2),
                "hardGateFailed": any(bool(v.get("hardGateFailed")) for v in vjs),
                "hardGateReasons": [],
                "reworkList": [],
                "rationale": "; ".join(str(v.get("rationale", ""))[:200] for v in vjs if v.get("rationale")),
                "verifierCount": len(vjs),
            }
            seen_reasons, seen_rework = set(), set()
            for v in vjs:
                for gr in v.get("hardGateReasons") or []:
                    key = str(gr)[:120]
                    if key not in seen_reasons:
                        seen_reasons.add(key)
                        merged["hardGateReasons"].append(gr)
                for item in v.get("reworkList") or []:
                    key = json.dumps(item, sort_keys=True, ensure_ascii=False)[:200]
                    if key not in seen_rework:
                        seen_rework.add(key)
                        merged["reworkList"].append(item)
            # v15.4 E-23：rework 清单按 priority 排序（高→中→低）
            order = {"高": 0, "中": 1, "低": 2}
            merged["reworkList"].sort(key=lambda it: order.get(str(it.get("priority", "中")), 1))
            return merged

        verify_jobs = [(sid, ei, vi) for sid, asg in assignment.items()
                       for ei in range(len(asg.get("execs", [])))
                       for vi in range(len(asg.get("verifiers", [])))]
        verdicts_raw = {}   # (sid, exec_idx) -> [(vmodel, vj, vplan, out_hash)]
        vworkers = min(8, max(1, len(verify_jobs)))
        with _TPE(max_workers=vworkers) as ex:
            for result in ex.map(_verify_one, verify_jobs):
                if not result:
                    continue
                sid, exec_idx, vmodel, vj, vplan, out_hash = result
                cost_log.append(vplan)
                verdicts_raw.setdefault((sid, exec_idx), []).append(
                    {"verifier": vmodel, "vj": vj, "plan": vplan, "outputHash": out_hash})

        # ---- v15.5 防线④：替补补位——某路输出全部 verifier 解析失败时，
        # 从异厂商候选补派一个替补 verifier（保证验证者数量不缩水） ----
        for sid, asg in assignment.items():
            exec_vendors_sub = {e["vendor"] for e in asg.get("execs", [])}
            used_vs = {x["verifier"] for recs in verdicts_raw.values() for x in recs}
            for ei in range(len(asg.get("execs", []))):
                if (sid, ei) in verdicts_raw:
                    continue  # 已有有效 verdict
                rec = outputs_raw.get((sid, ei))
                if not rec or not rec.get("text"):
                    continue
                sub_verifiers = asg.get("verifiers", [])
                sub_vvendors = {v["vendor"] for v in sub_verifiers}
                sub_cids = {v["cid"] for v in sub_verifiers}
                for cid, cand in caps.get("models", {}).items():
                    vb = cand.get("baseModel")
                    vv = _vendor_of_cand(cand)
                    if vv in exec_vendors_sub or vv in sub_vvendors:
                        continue
                    if cid in sub_cids or cid in used_vs:
                        continue
                    if cand.get("provider") in {e["cand"].get("provider") for e in asg.get("execs", [])}:
                        continue  # hetero 原则：替补也必须与执行者异厂商
                    # 替补 verifier 加入 assignment 并立即验证这一路
                    asg["verifiers"].append({"cid": cid, "vendor": vv,
                                             "base": vb,
                                             "cand": cand, "cost": 0.0})
                    sub_job = (sid, ei, len(asg["verifiers"]) - 1)
                    try:
                        sub_res = _verify_one(sub_job)
                        if sub_res:
                            s_sid, s_ei, s_vmodel, s_vj, s_vplan, s_hash = sub_res
                            cost_log.append(s_vplan)
                            verdicts_raw.setdefault((sid, ei), []).append(
                                {"verifier": s_vmodel, "vj": s_vj, "plan": s_vplan,
                                 "outputHash": s_hash})
                            _log(run_dir, "rounds.jsonl",
                                 {"event": "verifier_substitute", "round": r, "subtask": sid,
                                  "execIdx": ei, "substitute": s_vmodel})
                    except Exception as e_sub:
                        _log(run_dir, "rounds.jsonl",
                             {"event": "verifier_substitute_failed", "round": r,
                              "subtask": sid, "execIdx": ei, "error": str(e_sub)[:200]})
                    break

        rows = []
        rework_all = []
        gate_failed_any = False
        feedback_rows = []
        fx_rate = selector.load_fx_rate().get("usdToCny")
        for sub in subtasks:
            sid = sub["id"]
            asg = assignment.get(sid) or {}
            # E-20 双路互评：每路输出合并各自 verifier 意见 → 取分高者为正式产出
            best = None
            best_rec = None
            for ei, e in enumerate(asg.get("execs", [])):
                recs = verdicts_raw.get((sid, ei), [])
                if not recs:
                    continue
                vjs = [x["vj"] for x in recs]
                merged = _merge_verdicts(vjs)
                if merged is None:
                    continue
                # E-22：指纹校验——verdict 评的必须是当前这路输出（orchestrator 绑定，hash 不符即无效）
                cur = outputs_raw.get((sid, ei)) or {}
                if cur.get("hash") and any(x.get("outputHash") and x["outputHash"] != cur["hash"] for x in recs):
                    merged["outputHashMismatch"] = True
                if best is None or merged["overallScore"] > best["overallScore"]:
                    best, best_rec = merged, (ei, e, recs)
            if best is None:
                continue
            ei, e, recs = best_rec
            outputs[sid] = (outputs_raw.get((sid, ei)) or {}).get("text", "")
            verdicts[sid] = best
            rows.append({"subtask_id": sid, "verifier": "+".join(sorted({x["verifier"] for x in recs})),
                         "score": float(best.get("overallScore") or 0)})
            if best.get("hardGateFailed"):
                gate_failed_any = True
            for item in best.get("reworkList") or []:
                rework_all.append({"subtask": sid, **item})
            # feedback_ring（H1）：scoredBy 改列表（v15.4 E-18）
            ex_rec = exec_results.get((sid, ei)) or {}
            ex_meta = ex_rec.get("meta") or {}
            # v15.5 问题9：被拒样本隔离——feedback 只收池内 active 成员（退役模型不写反馈）
            fb_model = _parse(e["cid"])[0]
            if not pool.is_member(fb_model):
                _log(run_dir, "rounds.jsonl",
                     {"event": "feedback_rejected_nonpool", "round": r,
                      "subtask": sid, "model": fb_model,
                      "reason": "模型不在池名单（已退役）→ 不写反馈环（污染隔离）"})
                continue
            feedback_rows.append({
                "run_id": run_id, "case_id": sid,
                "model": _parse(e["cid"])[0],
                "thinking": _parse(e["cid"])[1],
                "scoredBy": sorted({x["verifier"] for x in recs}),
                "verifierScore": float(best.get("overallScore") or 0),
                "success": True,
                "hardGateHit": bool(best.get("hardGateFailed")),
                "reworkTriggered": False,
                "taskVector": subtasks_map.get(sid, {}),
                "latency_ms": int((ex_meta.get("elapsed_s") or 0) * 1000),
                "cost_usd": round((ex_rec.get("actual") if ex_rec.get("actual") is not None else ex_rec.get("plan", 0.0)) / fx_rate, 8) if fx_rate else None,
                "usage": ex_meta.get("usage"),
                "ts": config_loader.now_shanghai().isoformat(),
            })

        # v15.5 覆盖修复：本轮有子任务完全无输出（no_candidate）→ 记硬门禁（触发返工/stalled）
        for sub in subtasks:
            _sid = sub["id"]
            if _sid not in outputs and not (assignment.get(_sid) or {}).get("execs"):
                gate_failed_any = True
                rework_all.append({"subtask": _sid,
                                   "target": "子任务未执行（no_candidate）",
                                   "issue": "选择器未能为本子任务分配执行者（候选/配额不足）",
                                   "expected": "扩充候选池或降低双路/子任务规模",
                                   "priority": "高"})
                _log(run_dir, "rounds.jsonl",
                     {"event": "subtask_uncovered_hardgate", "round": r, "subtask": _sid})

        agg = calibration.aggregate(rows)
        s_r = agg.get("S_r", 0.0)
        terminator.advance(st, s_r)
        _log(run_dir, "rounds.jsonl",
             {"event": "round_end", "round": r, "S_r": s_r, "subtask_scores": agg["subtask_scores"],
              "hardGateFailed": gate_failed_any})

        # ---- deciding：terminator（v15.4 成本 forced 已删，cost 仅审计记录） ----
        rework_hash = json.dumps(sorted(rework_all, key=lambda x: json.dumps(x, sort_keys=True)))[:200]
        cost_so_far = sum(cost_log)
        wall_elapsed = time.time() - wall_start
        action, reason = terminator.decide(st, gate_failed_any, rework_hash,
                                           params["theta"], cost_so_far, 0.0,
                                           wall_elapsed_s=wall_elapsed,
                                           wall_budget_s=wall_budget_s)
        _log(run_dir, "rounds.jsonl", {"event": "decision", "round": r, "action": action, "reason": reason})
        # v15.1 修正：reworkTriggered 语义 =「本轮真的返工了该子任务」（terminator 决策），
        # 而非「verifier 列了清单」——否则已收敛 run 的反馈被误排除，自进化样本永久偏少。
        rework_subtasks = {item["subtask"] for item in rework_all} if action == "rework" else set()
        for row in feedback_rows:
            row["reworkTriggered"] = row["case_id"] in rework_subtasks
            _append_feedback(row)
        if action in ("converged", "early_stop", "forced", "stalled"):
            # L1：终止全量审计（v15.4：成本字段仅审计记录，不参与判据）
            _log(run_dir, "rounds.jsonl",
                 {"event": "termination_audit", "action": action, "reason": reason,
                  "round": r,
                  "costSoFarCny": round(cost_so_far, 4),
                  "wallElapsedS": round(wall_elapsed, 1), "wallBudgetS": wall_budget_s,
                  "sHistory": st.s_history, "callCount": len(cost_log)})
            break
        # rework：把清单写进下一轮上下文
        if rework_all:
            (run_dir / f"rework-r{r}.json").write_text(
                json.dumps(rework_all, ensure_ascii=False, indent=2), encoding="utf-8")

    # ---- 3.5 自进化：反馈 → 能力档案（H1，文件锁 + 原子写 + 写前校验 + revision 自增） ----
    # 失败不静默：错误进 rounds.jsonl 与 result.json（宿主工具/控制台可见），退出码不因档案更新失败而降级
    upd = None
    upd_error = None
    fb_params = all_params.get("feedback", {})
    # v15.4b：遥测必须先于 pending diff——此前顺序相反，遥测回填改档案 → pending 的
    # baseHash 立即失效 → 次日自动 apply 被 baseHash_mismatch 永久拒绝（04:30 nightly 实测）。
    try:
        tele = update_capabilities.update_runtime_telemetry()
        _log(run_dir, "rounds.jsonl", {"event": "runtime_telemetry_updated", **tele})
    except Exception as e:
        _log(run_dir, "rounds.jsonl", {"event": "runtime_telemetry_failed", "error": str(e)[:200]})


    try:
        # v15.4（the maintainer decided全程无人值守）：统一写 pending diff——autoApply=true 时由插件
        # 每日 04:30 漂移体检通过后自动 --apply（apply_pending 自带体检拒绝 + baseHash 校验）
        upd = update_capabilities.pending_diff()
        _log(run_dir, "rounds.jsonl", {"event": "capabilities_updated", **upd})
    except Exception as e:
        upd_error = str(e)[:300]
        _log(run_dir, "rounds.jsonl",
             {"event": "capabilities_update_failed", "error": upd_error})

    # P1-3：Elo 横向比较（独立于能力档案回写；失败不阻断）
    try:
        pw = pairwise.update()
        _log(run_dir, "rounds.jsonl",
             {"event": "elo_updated", "pairComparisons": pw.get("pairComparisons"),
              "ratings": pw.get("ratings")})
    except Exception as e:
        _log(run_dir, "rounds.jsonl", {"event": "elo_update_failed", "error": str(e)[:200]})

    # ---- 4. synthesize ----
    combined = ""
    for sub in subtasks:
        combined += f"\n### {sub['title']}\n{outputs.get(sub['id'], '(缺)')}\n"
    combined += "\n\n验证结论：" + json.dumps(verdicts, ensure_ascii=False)
    # v15.5 3C：synthesize 角色动态化（回退默认）
    synth_cid = _pick_role(caps, SYNTH_WV, "synthesize")
    smodel, sthink = tuple(synth_cid.rsplit("__", 1)) if synth_cid else SYNTH_DEFAULT
    _log(run_dir, "decisions.jsonl",
         {"event": "role_assign", "role": "synthesize",
          "model": smodel, "thinking": sthink,
          "weightVector": SYNTH_WV,
          "reason": f"3C 角色动态选择（候选 {synth_cid or '无→默认'}）"})
    try:
        final, smeta = stream_llm.call_stream(
            smodel, sthink, SYNTH_PROMPT.format(FRESHNESS_RULES=FRESHNESS_RULES,
                                                task=task, combined=combined),
            config_loader.max_tokens_for_model(smodel))
        if smeta.get("timeout_kind") or smeta.get("finish_reason") == "error":
            selector.record_failure(smodel)
        else:
            selector.record_success(smodel)
        su = smeta.get("usage") or {}
        token_profiles.record(smodel, "synthesize", su.get("promptTokens"),
                              su.get("completionTokens"), su.get("cacheHitTokens") or 0)
        sest_in, sest_out = token_profiles.est_for(
            smodel, "synthesize", selector.estimate_tokens(combined) + 400, 1500)
        scost, splan, sactual = _cost_pair(smodel, sthink, sest_in, sest_out,
                                           meta=smeta, balance=balance, role="synthesize")
        _log(run_dir, "cost.jsonl",
             {"role": "synthesize", "model": smodel, "thinking": sthink,
              "usage": smeta.get("usage"), "estCostCny": round(scost, 6),
              "planCostCny": round(splan, 6),
              "actualCostCny": round(sactual, 6) if sactual is not None else None})
        cost_log.append(splan)
    except Exception as e:
        final = f"## 结论\n\n综合阶段失败：{e}\n\n（子任务输出与验证记录见 run 目录）"
        selector.record_failure(smodel)
    report_path = run_dir / "report.md"
    report_path.write_text(final, encoding="utf-8")
    cost_so_far = sum(cost_log)
    _log(run_dir, "decisions.jsonl",
         {"event": "final", "rounds": len(st.s_history), "s_history": st.s_history,
          "action": action, "reason": reason, "cost_so_far": round(cost_so_far, 4),
          "costLog": [round(c, 6) for c in cost_log],
          "capabilitiesUpdate": upd, "feedbackRows": len(feedback_rows)})

    result = {"status": action, "run_dir": str(run_dir), "rounds": len(st.s_history),
              "warnings": _warnings,
              "s_history": st.s_history, "report": str(report_path),
              "tier": tier, "mode": mode,
              "cost_so_far": round(cost_so_far, 4),
              "feedback_rows_written": len(feedback_rows),
              "capabilities_revision": (upd or {}).get("revision"),
              "capabilities_update": {"ok": upd is not None, "error": upd_error,
                                      "autoApply": bool(fb_params.get("autoApply")),
                                       "pendingDiff": bool((upd or {}).get("pendingDiff")),
                                       "changedScores": (upd or {}).get("changedScores"),
                                      "skipped": (upd or {}).get("skipped", False),
                                      "sourceRunIds": (upd or {}).get("sourceRunIds")}}
    # v15.4 A-3：汇率 fallback 链状态告知主会话（不再停机拒绝）
    if fx_warning:
        result["fx_warning"] = fx_warning
    if mode == "inline":
        result["inline_text"] = final
    return _finish(result, exit_code=0)

def _parse(cid: str):
    model, thinking = cid.rsplit("__", 1)
    return model, thinking

def _gather_deps(sub, outputs):
    deps = sub.get("dependencies") or []
    return "\n".join(f"[{d}] {outputs.get(d, '(缺)')[:500]}" for d in deps if d in outputs)

def main():
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("--task", "-t", required=True)
    ap.add_argument("--tier", "-p", default="standard", choices=["fast", "standard", "deep"])
    ap.add_argument("--mode", "-m", default="report", choices=["report", "inline"])
    ap.add_argument("--dry", action="store_true")
    ap.add_argument("--facts", default=None, help="宿主权威状态快照文件路径（v15.5c，附加到任务文本）")
    args = ap.parse_args()
    try:
        result = run_council(args.task, args.tier, args.mode, args.dry, args.facts)
    except Exception as e:
        print(json.dumps({"status": "council_error", "error": str(e)[:500]},
                         ensure_ascii=False, indent=2))
        sys.exit(1)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    # M3：失败路径退出码非 0（宿主 shell.run 据此判定，杜绝静默回退旧报告）
    sys.exit(result.get("_exitCode") or 0)

if __name__ == "__main__":
    main()
