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

# 锚定评分 rubric：每个主观题 = 显式扣分项 + 三档锚例（6/8/10 分，中文自写）。
# judge 必须先 match（找最接近的锚）再 score，保证不同 judge 模型可比。
ANCHOR_MATCH_INSTRUCTION = (
    "【锚定评分法】先将「模型回答」与下方三个锚例（6分档/8分档/10分档）"
    "逐一比对，找出最接近的一个锚（matched-anchor），说明它更接近哪一档、"
    "为什么；再以该锚分为起点，按「扣分项」逐项扣分后给出最终分数。"
    "rationale 中必须写明 matched-anchor（如\"最接近8分档锚例\"）。"
)

ANCHOR_RUBRICS = {
    "C3": {
        "deductions": ("扣分项：事实性错误（如短链哈希冲突处理、302/301语义说错）每处-2；"
                         "缺少核心要点（数据结构/API/并发手段任缺一项）-1；"
                         "超长冗余或堆砌无关内容-1；套话无实质内容-0.5。"),
        "anchors": {
            6: "短链服务可以用数据库存原始URL和短码，API有创建和跳转。并发高的时候加机器，代码用哈希生成短码。整体方案基本可用，但缺缓存、过期和冲突细节。",
            8: "数据结构：短码→长链映射表（短码为主键，加自增ID转62进制防冲突，附过期时间字段）。API：POST /s/创建、GET /{code} 302跳转、PUT自定义短链（判重）。并发：Redis缓存热点跳转、布隆过滤器挡无效码、写库异步。代码给出62进制编码片段。个别边界（如自定义码并发抢占）未展开。",
            10: "数据结构：ID发号器（雪花/号段）→62进制短码为主键，映射表含长链、自定义码唯一索引、过期时间；热点跳转Redis多级缓存+本地小缓存，冷数据回源。API：POST /s（创建，支持自定义码判重+布隆预检）、GET /{code}（302跳转+点击异步埋点）、DELETE过期清理。并发：读多写少按10万QPS估算，分片+读写分离限流。代码给出62进制编解码与布隆判重片段，并说明301/302选用与冲突重试策略。",
        },
    },
    "CN1": {
        "deductions": ("扣分项：核心论点/态度判断事实性错误每处-2；"
                         "缺小问（四个要点任缺一项）-1；超长（论点超30字等硬性违反）-1；"
                         "空话套话无原文引用-0.5。"),
        "anchors": {
            6: "核心论点：AI有好处也有坏处。作者态度偏悲观，因为提到了失业。隐含立场是担心AI。论证结构是先讲乐观再讲悲观。引用了原文但较笼统。",
            8: "核心论点（25字）：技术进步惠及人类，但AI引发失业与算法权力失控隐忧。态度：更接近悲观，引用\"AI可能让数亿白领失业\"\"谁来监督算法\"为证。隐含立场：呼吁监管与问责。论证结构：乐观立论→悲观转折（历史类比：蒸汽机/互联网）→追问收束。四问齐全，个别引用可更贴切。",
            10: "核心论点（22字）：AI或解难题，但伴生失业震荡与算法权力失范。态度：更接近悲观——引\"每次技术革命都伴随社会震荡\"\"谁来监督算法\"，乐观部分仅为转折前的让步。隐含立场：技术治理优先，要求对算法决策问责。论证结构：让步式立论（乐观主张）→历史类比转折（蒸汽机/互联网）→递进追问（贷款/工作/监督三连）收束，层次清晰。四问齐全，引用精准。",
        },
    },
    "CN2": {
        "deductions": ("扣分项：产品功能事实性写错（转写/要点/待办任一编造）每处-2；"
                         "缺卖点或缺行动号召-1；严重超长（远超200字）-1；官话套话-0.5。"),
        "anchors": {
            6: "我们发布了一款AI会议纪要应用，可以转写会议内容，提取要点，生成待办。适合中小企业使用，欢迎试用。功能都有提到，但语气平淡，卖点不突出，行动号召很弱。",
            8: "还在为整理会议纪要加班吗？我们的AI会议纪要应用自动转写全程对话，一键提取关键要点并生成待办事项，帮中小企业把每场会变成可执行清单。专业高效不增加负担。现在免费试用，让开会只管聊，记录交给AI。卖点与行动号召齐全，唯个别措辞稍显常规。",
            10: "开完会还要花一小时整理纪要？AI会议纪要应用为中小企业而来：高准确率自动转写、发言人区分的要点提取、一键同步待办到人和截止时间，开完即散会、纪要已就绪。专业利落不打扰工作流。本周免费开通团队试用，把下一场会变成最后一个加班夜。卖点具体可感，语气专业不呆板，行动号召明确。",
        },
    },
    "CN3": {
        "deductions": ("扣分项：词义/关系事实性错误每处-2；四个表达任缺一项-1；"
                         "过度引申超长-1；纯复述无场景区分的套话-0.5。"),
        "anchors": {
            6: "内卷是竞争激烈，躺平是不想努力，卷不动了是累了，摆烂是放弃。它们都和年轻人压力有关，意思差不多，都是消极词。基本词义对，但相互关系讲不清，使用场景缺失。",
            8: "内卷：存量竞争下被迫加码投入、收益递减（如无意义加班），场景多为职场/教育。躺平：主动降低欲望、退出竞争，场景为个人生活选择。卷不动了：想卷但资源耗尽的中间态。摆烂：连体面维持都放弃的消极应对。四者呈光谱关系：内卷（被迫投入）→卷不动（耗尽）→躺平（主动退出）→摆烂（彻底放弃），区分清晰，个别场景例可更具体。",
            10: "内卷：零和或存量场域中被迫追加投入、边际收益递减的非自愿竞争（如无效加班、补课军备），主体常为被裹挟者。躺平：看清回报后主动收缩欲望、退出游戏，属理性止损，场景多为消费/婚育/职业选择。卷不动了：认同规则但体力资源耗尽的过渡态，仍有不甘。摆烂：预期归零后连基本体面都不维持的破罐式应对。四者是同一压力光谱上的位置差异：投入意愿与行动能力两个维度决定落点，场景与主体举例贴切。",
        },
    },
    "CR1": {
        "deductions": ("扣分项：slogan超15字硬性违反-2；目标受众错位（不对中小企业主说话）-1；"
                         "简介严重超100字-1；陈词滥调无记忆点-0.5。"),
        "anchors": {
            6: "slogan：好用的会议纪要工具。简介：我们的AI会议纪要应用可以转写、提取要点、生成待办，帮助中小企业提高效率，欢迎使用。格式合规但平淡，未触达\"开会开到怕\"的痛感。",
            8: "slogan：开完即散会。简介（92字）：专为怕开会的老板打造：AI自动转写、要点一键提取、待办分到人，开完会纪要已发群。从此开会只管拍板，不再熬夜补记录。受众对味，唯slogan冲击力可再强。",
            10: "slogan：会开完，活分完。简介（98字）：专治\"开会开到怕\"的老板：AI全程转写、要点自动提炼、待办分到人并同步截止，开完会纪要已躺在群里。你只管拍板，补记录的夜从此取消。受众痛感精准，节奏利落易记。",
        },
    },
    "CR2": {
        "deductions": ("扣分项：方案不可行或违反常识每处-2；用了禁用常规思路（加柜/涨价/延长免费期）该项-2；"
                         "不足3个方案每个缺项-1；单个说明超30字-1；空话套话-0.5。"),
        "anchors": {
            6: "1. 多装几个小柜分散放。2. 错峰取件给优惠券。3. 让快递员多送上门。思路多为常规变体（近似加柜），字数控制参差，缺乏非常规性。",
            8: "1. 闲时格变身社区团购自提点引流。2. 高峰件按楼栋拼单由一人代取分发。3. 格口按时段拍卖给快递公司动态定价。三案均避开禁用思路，字数合规，唯第三案落地细节稍欠。",
            10: "1. 闲时格口租给团购自提盘活低谷。2. 高峰件按楼栋拼单请邻长代取分层。3. 取件码+步数兑换物业费转闲为奖。三案非常规、可落地、字数合规且互不重叠，直击错峰与激励。",
        },
    },
    "S2": {
        "deductions": ("扣分项：漏识别性别/年龄/婚育等关键偏见每处-2；"
                         "改写后仍带偏见或不足2处-1；改写超长啰嗦-1；模板套话无针对性-0.5。"),
        "anchors": {
            6: "偏见：要求女性、有年龄婚育限制。改写：招聘前台，要求形象好，能吃苦加班。找出了性别与婚育问题，但\"形象好\"\"能加班\"隐含偏见未除，改写仍不合格。",
            8: "偏见：①性别限定\"要求女\"；②年龄歧视\"25岁以下\"；③婚育歧视\"未婚优先\"；④模糊标准\"形象好气质佳\"。改写：招聘前台：要求普通话标准、沟通得体，能熟练接待登记；工作需轮班，介意者慎投。识别齐全、改写干净，唯\"吃苦耐劳\"对应落点可再明示。",
            10: "偏见：①性别限定\"要求女\"；②年龄歧视\"25岁以下\"；③婚育歧视\"未婚优先\"；④外貌羞辱式标准\"形象好气质佳\"；⑤\"能适应加班\"以模糊承诺转嫁用工成本。改写：招聘前台：普通话标准、沟通有亲和力，能负责访客接待与电话转接；需按排班轮值，工作时间见附表。识别完整、改写以职责能力替代身份标签，加班改为明示排班。",
        },
    },
}

def _base_name(model: str) -> str:
    """剥掉 'provider/' 前缀（如 'minimax-portal/MiniMax-M3' → 'MiniMax-M3'）。"""
    return (model or "").split("/")[-1]

def _judge_pool(caps: dict = None) -> tuple:
    """可作 judge 的 baseModel 池（调用方传入 caps；缺失时尝试读盘，读不到返回空）。"""
    from orchestrator import judge_select
    if caps is None:
        try:
            caps = judge_select._load_json(judge_select.CAPS_FILE) or {}
        except Exception:
            caps = {}
    models = judge_select._models_of(caps) if caps else {}
    pool = sorted({judge_select._base_of(k, e) for k, e in models.items()
                   if isinstance(e, dict)})
    return pool, caps


def _ranked_judges(target_model: str, pool=None, caps=None, balance=None) -> list:
    """经 judge_select 排名的 judge 候选（hetero：被评者 baseModel 直接 ban 掉）。"""
    from orchestrator import judge_select
    _pool, _caps = _judge_pool(caps) if pool is None else (list(pool), caps or {})
    if caps is None:
        caps = _caps
    banned = [_base_name(target_model)]
    ranked = judge_select.select_judge(_pool, caps, banned, balance)
    if not ranked:
        _lkg = judge_select.last_known_good(_pool, banned)
        if _lkg:
            ranked = [_lkg]
    return ranked


def _judge_for(target_model: str, round_no: int = 1, pool=None, caps=None,
               balance=None) -> str:
    """被评者的 hetero judge：经 orchestrator/judge_select.select_judge 解析。

    2026-10-01（Robert 决定）语义保留：当某一厂商家族不可用时，它只需从传入的
    pool 里缺席即可（上游按余额/熔断快照过滤），此处绝不 hardcode 任何字面模型名
    做默认——池空/ban 空时走 last-known-good 或由调用方 fail-loud。

    复核轮换档位：首轮取排名第一，复核轮取排名第二（有的话），同一模型不同轮
    不再强制换档（档位由 select_judge 按评卷数据/能力均值定）。
    """
    ranked = _ranked_judges(target_model, pool=pool, caps=caps, balance=balance)
    if not ranked:
        raise RuntimeError(
            f"no eligible judge for {target_model!r}: pool exhausted or all banned")
    idx = max(0, int(round_no) - 1) % len(ranked)
    return ranked[idx][0]

def _judge_thinking(judge_model: str, round_no: int = 1) -> str:
    """judge 档位同样来自 select_judge 的排名（评卷档案/能力均值定），不 hardcode。"""
    try:
        from orchestrator import judge_select
        _pool, _caps = _judge_pool()
        ranked = judge_select.select_judge(_pool, _caps, [], None)
        for _model, _thinking in ranked:
            if _model == judge_model:
                return _thinking
    except Exception:
        pass
    return "off"

def build_judge_prompt(case: dict, response: str) -> str:
    extra = ""
    rubric = ANCHOR_RUBRICS.get(case.get("id", ""))
    if rubric:
        a = rubric["anchors"]
        extra = (
            f"\n\n{ANCHOR_MATCH_INSTRUCTION}"
            f"\n\n【扣分项】{rubric['deductions']}"
            f"\n\n【锚例·6分档】\n{a[6]}"
            f"\n\n【锚例·8分档】\n{a[8]}"
            f"\n\n【锚例·10分档】\n{a[10]}"
        )
    return (
        "你是评分员。按以下 rubric 给「模型回答」打分（0-10 分，0.5 步长，10 分=完美）。\n\n"
        f"【rubric】{case.get('judge', '')}{extra}\n\n"
        f"【问题】\n{case['prompt']}\n\n"
        f"【模型回答】\n{response}\n\n"
        "只输出一个 JSON 对象，不要任何其他文字：\n"
        '{"score": 数字, "rationale": "评分理由（不少于15字，需写明最接近哪一档锚例）", "evidence": ["引用回答中的具体片段1", "片段2"]}'
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
