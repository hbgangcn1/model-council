# Model Council v14 设计：能力感知的动态收敛 Council

> 状态：**设计基线（v15.4 定稿 + v15.5 决议记录，v15.6 基线确认）**。本文档为设计基线 + 拍板记录；实施细节以代码为准。
> v15 修复、v15.1 收尾、v15.2/v15.3 元评审见 §14；v15.4 成本哲学重构 + benchmark 自进化见 §14-v15.4；v15.5 计划与实施对照见 `roadmap.md` 台账（计划未线性执行，以台账为准）。
> **v15.6+ 演进（L3 传输抽象、judge 并行/活性门、额度分类、预置快照、运维手册）未归档进本文**——看 CHANGELOG（§15.6–15.9）、ADR-004 与 `operations.md`。
> v15 修复（2026-08-24 评审 H1-H4/M1-M4/L1-L3）、v15.1 收尾、v15.2 元评审 13 项、v15.3 元评审 7 项见 §14；v15.4 成本哲学重构 + benchmark 自进化设计决议见 §14-v15.4；**v15.5 递归评审架构 + 判停标准重构见 §14-v15.5**（实施路线见 `v15.5-roadmap.md`）。
> 文档与实现的出入点已在本稿同步修订（§3/§5/§5.2/§6/§7.1a/§9/§13）；§1-§13 中与 v15.4/v15.5 决议冲突处已内联标注「v15.4 修订」/「v15.5 修订」，以对应版本章节为准。
> 参考：微软 Agent Framework 的 Selection/Termination Strategy、evaluator-optimizer 编排；Leige GameDev skill 的验收治理（角色分离、硬门禁、修订号、返工清单）。

## 0. 决策记录（2026-08 讨论拍板）

| # | 决策点 | 结论 |
|---|--------|------|
| D1 | 落地形态 | **重构 Python orchestrator（council.py）** 承载收敛循环；SKILL.md 只描述策略与配置 |
| D2 | 静态角色表 | **完全动态化**：角色→模型分配全部由评分函数决定；presets 从「角色表」改为「风险档位 → 预算/门禁约束」 |
| D3 | 收敛判据 | **动态轮数**：硬门禁 + 绝对阈值 + **边际收益早停**（返工后分数提升微乎其微就停）+ 轮数上限兜底（~~预算上限~~ v15.4 已删）。**（v15.5 修订：轮数上限删除、θ 三档统一 9.5、早停改「近 3 轮窗口极差 <0.2」，见 §14-v15.5-A）** |
| D4 | 模型粒度 | **同一底层模型 × 不同 thinking 档位 = 不同模型**（reasoning off 也算一档）。能力档案、评分、护栏全部按 `provider/model@thinking` 粒度执行 |

## 1. 目标与设计原则

**目标**：把固定管线（Decompose → Review → Cross-Review → Synthesize）升级为「按模型能力动态调度、逐轮评估、收敛才输出」的循环。

**原则**：

1. **数据驱动**：`capabilities.json`（benchmark + 运行期反馈合成）是选择策略的唯一数据源，不许拍脑袋选模型。
2. **完全动态分配 + 安全护栏**：分配自由（哪个模型干什么由评分决定），但安全边界是硬约束（见 §5），护栏不是角色表。
3. **收敛才输出**：质量闸门在 Verifier，不在流水线顺序；没达标就带着清单返工。
4. **成本是公民（v15.4 修订）**：成本进入评分函数（λ·cost，温和微调、同分 tie-break）与余额耗尽护栏，**不再做终止判据**——算钱是为选模型（能力强、性价比高、够钱跑），不做用量控制（v15.4 核心原则，maintainer 拍板）。
5. **全程可审计**：每次模型调度写一条「为什么选它」日志（能力分、成本、得分算式）。
6. **隔离验证**（Leige 原则「写码的不验收」）：验证者与被验证者**不可能是同一模型**，这是硬约束。

## 2. 总架构

```
                    ┌───────────────────────────────┐
                    │  运行期反馈（runtime-feedback）   │
                    └──────────────┬────────────────┘
                                   ↓  每次 council 收尾融合（§3.5）
 [benchmark/scores] ──→ capability_ingest（diff 审批 → v15.4 起自动合入）──┐
                                                          ├─→ capabilities.json（唯一决策数据源）
 [build_capabilities（基准生成）] ──────────────────────────┘
                                   │
 [任务] → Router（风险档位：门禁阈值/λ/maxSubtasks，council-params.json；v15.4 删预算上限；**v15.5 删 maxIter、θ 统一 9.5**）
        → Decomposer（拆子任务 + 每子任务维度权重向量，**不再指定模型**）
        → 选择策略（评分函数 + 六道护栏过滤）→ 每子任务分配执行者 + 动态验证者（v15.4）
        → 第 r 轮执行 → Verifier：整体分 S_r + 硬门禁检查 + rework 清单
              │
              ├─ 收敛判据通过 ──────→ Synthesizer → 报告（含决策审计+成本记录）
              ├─ 边际收益枯竭/上限 ──→ 按当前最佳合成，标注置信度
              └─ 未通过 ────────────→ 第 r+1 轮（只执行 rework 清单上的项，增量）
```

## 3. 能力档案 capabilities.json

**模型粒度（D4）**：档案条目的 key 是 `provider/model@thinking`——同一底层模型的不同 thinking 档位是**独立条目**，各有各的分数、成本、延迟档案。`reasoning off`（@off）也是一档。

- 依据：thinking 档位显著影响质量、延迟（120s 超时风险）与成本（thinking tokens）。
- **档位枚举按模型自定，不统一**（实测选择器；**v15.5 修订：档位枚举一律以 host-bridge 运行时模型目录为准（插件桥 `model-tier-bridge.json`），council 不再自定档位表**——DeepSeek = off/low/high/max，pi-ai 系按 thinkingLevelMap）：

  | 底层模型 | 档位枚举 |
  |---|---|
  | MiniMax-M3 | off / minimal / low / medium / high（5 档） |
  | deepseek-v4-pro | off / low / high / max（4 档） |
  | deepseek-v4-flash | off / low / high / max（4 档） |

  未来新增模型**其档位表由 host-bridge 模型目录提供**（v15.5-J-22）；护栏按各模型实际档位判断，而非全局统一枚举。
- 每个条目保留 `baseModel` 字段，供多样性/自验护栏按底层模型判断（见 §5）。

**生成（实际实现，v15.1）**：三个 Python 入口共同维护 `capabilities.json`：

1. **`benchmark/build_capabilities.py`**：从 `benchmark/scores-summary.json` 生成初始档案（~~实测档位 + 插值档位，插值档标记 `interpolated: true`~~ **v15.5 修订：插值废除，全档位实测；档位表与 wire 协议从插件桥读取**）。
2. **`benchmark/capability_ingest.py`**：把 `benchmark/scores/<cid>/*.json` 的逐题分按维度 EMA 并入档案（`_source_run_ids` 可回溯；**默认走 diff 审批，`--apply` 合入**，见 §14-L1；**v15.4 起改全自动合入（体检+门禁+熔断安全网），见 §14-v15.4-F-30**）。
3. **`orchestrator/update_capabilities.py`**：把 `evals/runtime-feedback.jsonl` 的 z-score 反馈贝叶斯融合进档案（每次 council 收尾自动调用，文件锁 + 写前校验 + revision 自增，见 §3.5）。

> **⚠️ 旧 benchmark 不可信（2026-08 审查结论）**：① judge gpt-5.5 与被评模型重叠，自评 11/50 案例只回「10/10」无理由、评别人写 144–206 字理由；② 评分协议未实施——实际是一句话打分 + 截断 3000 字符 + 异常记 5 分 + 0 分剔除；③ 盲评无产物；④ 两套分数差 2 分（v1 全员 8.7–9.5 vs v11 全员 6.76–7.97），有效区间仅 1.2 分；⑤ 冠亚军 kimi-k2.7-code/gpt-5.5 均非当前可用模型 → 数据双重失效。故重跑最小可信基准 v2.1，旧数据只做参考。

**Benchmark 分档策略**（**v15.5 修订：插值废除，全档位×全案例——maintainer 2026-08-25 拍板**。~~避免 全档位 × 全案例 的成本爆炸~~：实测发现推理档位与评分效果不成比例，线性插值不能反映真实能力，且 16 档中 4 档（v4-pro__low、v4-flash__low、M3__minimal、M3__low）的插值分数会污染 selector 与自进化反馈环。实测成本账：DeepSeek 6 档×28 题 ≈¥9.94、补 4 档增量 ≈¥2-3、新模型全档 ≈¥7-8、全量重跑 ≈¥15-25 + M3 额度——全档位×全案例的成本完全可接受）：
- 每个模型**全档位 × 全案例**实测（断点续传，缺档自动补跑）；~~每模型全量跑 2–3 个代表性档位~~、~~其余档位按「档位单调性假设」插值~~ 一律废止，档案不再出现 `interpolated: true` 条目。
- 档位枚举与 wire 协议以 **host-bridge 运行时模型目录为准**（`model-tier-bridge.json` 插件桥单一数据源，见 §14-v15.5-J）：DeepSeek 档位 = off/low/high/max（官方 `reasoning_effort` 协议）；pi-ai 系按 thinkingLevelMap；不再使用手填 budget_tokens 映射。
- max_tokens 还原模型自身上限（host-bridge `capabilityMaxTokens`/`defaultMaxTokens`），删除 32768/8192/16384 自定义值——实测 flash__high 单题思考 27.5K token，自定义上限会截断真实输出。
- 新模型入池协议：先跑全档位×全案例 benchmark 得初分，再入候选池。

**Schema**（实际 v2，条目 key = `model__thinking`——实现用 `__` 连接符而非早期草案的 `@`；**v15.5 起 `interpolated` 字段随插值废除消失**，下例为历史格式）：

```json
{
  "schemaVersion": 2,
  "revision": 2,
  "generatedAt": "2026-08-24T03:32:05+08:00",
  "updatedAt": "2026-08-24T13:27:02+08:00",
  "dimensions": ["reasoning","code","chinese","research","instruction_following","long_context","tool_use","creativity","safety"],
  "models": {
    "deepseek-v4-pro__high": {
      "baseModel": "deepseek-v4-pro",
      "thinking": "high",
      "provider": "deepseek-official",
      "tier": "T1-pay-per-token",
      "stable": true,
      "capabilities": {
        "reasoning": { "score": 8.3, "samples": 4, "freshness": 1.0,
                       "interpolated": false, "_source_run_ids": ["R1","R2","R3","R4"] }
      },
      "runtime": { "avgVerifyScore": null, "successRate": null, "samples": 0 },
      "cost": { "avgInputTokens": null, "avgOutputTokens": null, "costPerCallCny": null }
    }
  },
  "ingestMeta": { "source": "benchmark/scores", "lastIngestAt": "…", "ingestedCaseIdsTotal": 372, "emaAlpha": 0.3 },
  "runtimeFeedback": { "totalRuns": 1, "lastUpdateAt": "…", "sourceRunIds": ["2026-08-24_12-44-44"] }
}
```

- 9 个维度 = 6 主维（基准套件）+ tool_use/creativity/safety 三补充维。
- `freshness` 随时间衰减（如每 30 天下调 0.05，下限 0.5）：模型换新后分数自动「贬值」，促使重跑 benchmark。
- 分数合并公式（基准摄入）：`score = 旧分 × (1−α) + 新批均值 × α`（α=0.3）；运行期融合见 §3.5。

## 3.5 运行期反馈闭环（自动化）

运行期反馈是「后验」，修正 benchmark 的「先验」。设计原则：**零额外成本收集 + 维度归因 + 归一化防自强化 + 贝叶斯融合**。

### 反馈信号（orchestrator 天然产生，不需额外调用）

| 信号 | 来源 | 记入能力分 | 记入可靠性分 |
|---|---|---|---|
| Verifier 维度分（交叉评，非自评） | council 每轮验证 | ✅ 核心 | — |
| rework 触发（子任务被打回） | 收敛循环 | ✅ 负向 | — |
| 硬门禁命中（无证据结论等） | Verifier | ✅ 强负向 | — |
| fallback 触发（超时/失败降档） | 超时处理 | — | ✅ 负向 |
| idle 超时触发 | §6.5 流式 | — | ✅ 负向 |
| 成功/失败（API 错误、空响应） | API 调用 | — | ✅ 负向 |
| TTFT / 每秒 token 数 | §6.5 流式 | — | ✅（填 latency） |

### 收集（零成本）

每次 API 调用后，orchestrator 往 `evals/runtime-feedback.jsonl` 追加一行：

```json
{
  "ts": "...", "model": "deepseek-v4-pro@high",
  "taskVector": {"reasoning": 0.3, "research": 0.5, "chinese": 0.2},
  "verifierScore": 4.2, "hardGateHit": false, "reworkTriggered": false,
  "fallbackTriggered": false, "idleTimeout": false,
  "ttftMs": 1200, "tps": 45, "success": true
}
```

### 维度归因

用子任务的 `taskVector` 把一条反馈**按权重分解**到各维度：一个 50% research + 30% reasoning 的任务得 verifier 分 4.2，就给 research 记 0.5×4.2、reasoning 记 0.3×4.2。这样模型「在 code 上强、在 research 上弱」能被真实任务数据区分出来。

### 归一化（防自我强化跑偏）

- **相对化**：verifier 分只在「同一轮 council、同类任务、不同模型」之间比较（z-score），消掉任务难度与 verifier 松紧的绝对偏差。
- **只统计成功完成的调用**：失败的进可靠性分，不进能力分（否则「拒绝回答」被记 0 分会污染能力分）。
- **最小改动阈值**：runtime 分与 benchmark 分偏差 <0.5 时不覆盖，只更新置信度——防噪声抖动、防一个模型的简单任务导致虚高。

### 贝叶斯融合 + 自动化触发（实际实现：orchestrator/update_capabilities.py）

```
runtime_score = benchmark分 + z_avg × 2.5（clamp 0-10）
merged = benchmark分 × (1 − w×0.7) + runtime_score × w×0.7，其中 w = min(样本数/20, 1)
```

- runtime 样本 <20 → 主要信 benchmark（先验主导）；≥20 → runtime 主导（拐点 20 样本）。
- **触发时机**：每次 council 收尾自动跑一次；**无有效反馈时 skipped（不空转 revision）**。
- **最小改动阈值（v15.1 修正）**：|merged − benchmark分| < 0.5 **不写分**，只更新 `runtimeSamples`（防噪声抖动、防一个模型的简单任务导致虚高）；**翻排名门槛**：至少 10 条有效 council（有效 run 数）才允许 ≥0.5 分的改动。
- **写入纪律（v15.1）**：文件锁（`orchestrator/file_lock.py`，并发 council 互斥 + 死锁过期抢占）→ 写前校验（`orchestrator/caps_guard.py`：score ∈ [0,10]、revision 单调递增，坏数据拒绝落盘）→ 备份旧版 → 原子写（tmp + replace）→ `runtimeFeedback.sourceRunIds` 记录本次融合用到的 run_id。
- 每次写入带 `revision` 号（单调递增，旧版备份为 `capabilities.rev<N>.json.bak`，可回滚）。

## 4. 任务剖析（Task Profiling）

Decomposer 的输出从「指定 preferredModels」改为「给每个子任务打维度权重」：

```json
{
  "riskTier": "standard",
  "subtasks": [
    {
      "id": "subtask-1",
      "title": "市场规模与增长趋势",
      "weightVector": { "research": 0.5, "chinese": 0.3, "reasoning": 0.2 },
      "minQuality": 7.0
    }
  ]
}
```

- **不指定模型**：模型由选择策略统一分配（D2 完全动态化的落地）。
- `minQuality`：该子任务可接受的最低模型能力分（低于此的模型不进候选）。
- Router 的档位从「选角色表」改为「定约束」：

| 档位 | 门禁 θ_accept | λ | maxSubtasks | 典型用途 |
|---|---|---|---|---|
| fast | 9.5（v15.5 三档统一） | 温和值 | 3 | 日常低风险建议 |
| standard | 9.5（v15.5 三档统一） | 温和值 | 4 | 重要默认 |
| deep | 9.5（v15.5 三档统一） | λ≈0 | 4 | 高风险/不可逆/花钱 |

> **v15.4 修订**：原表含「预算上限 ¥0.03/0.15/0.35」列——**已删除**（预算不做用量控制，maintainer 拍板）。λ 数值改为温和化语义（能力分主导、同分 tie-break），具体值开工时定。
> **v15.5 修订**：θ 三档统一 9.5、**maxIter 列删除**（不设轮数上限，见 §14-v15.5-A）；档位 = 并行规模（maxSubtasks + 双路执行 + 验证者数），质量线不分档。

**档位参数外置（v15.1）**：上表数值存在 `council-params.json` 的 `tiers.*`（`--budget/--threshold` CLI 连续覆盖仍可用），调参不再改代码。

**预算 SLA（v15.1 文档化，月度复盘；v15.4 成本降为纯观察）**：每档的目标延迟/成本存 `council-params.json` 的 `sla.*`（v15.4 后成本字段只观察不判违规）：

| 档位 | 墙钟 p50 目标 | 墙钟 p95 目标 | 成本（v15.4 降为纯观察） |
|---|---|---|---|
| fast | ≤60s | ≤120s | ~~¥0.03~~ 观察指标 |
| standard | ≤600s | ≤1200s | ~~¥0.15~~ 观察指标 |
| deep | ≤1200s | ≤2400s | ~~¥0.35~~ 观察指标 |

> **v15.4 修订**：成本上限从「违规告警」降级为**纯观察指标**（继续统计供复盘和喂给估算优化，不判违规、不告警）；墙钟目标保留。墙钟预算本身 v15.4 统一为 1800s 技术防呆（见 §14-v15.4-D-13），本表 p50/p95 仍是文档化观察目标。

实测分布由 `orchestrator/sla_stats.py` 从 `runs/*/result.json` 计算（每档 p50/p95 墙钟与成本）落盘 `sla-report.json`；控制台「护栏与告警」Tab 与 `/metrics` 展示达标状态。月度复盘对比 observed vs target，偏差持续超限应校准 `params.sla` 或档位参数（四铁律：有数据依据 + 记录理由）。

**记账货币**：统一 **CNY（人民币）**——定价档案每个价格带声明 `currency` 字段，计算时按全局汇率（USD→CNY=7.2，月级手动更新）换算参与评分；报告双轨显示「原始币种 + CNY」。credits 类计费（Mimo/Copilot）在档案里声明 `tokenToCredits × creditsToCurrency` 两步换算链；额度类（OpenCode Go）边际成本为 0、不进预算计算（由 quotaFactor 管，订阅均摊只进报告的会计成本）。

**轮数上限**（**v15.5 修订：轮数上限已删除**——只追求最佳收敛，不设 maxIter/max_rounds；判停 = θ_accept=9.5 达标 / 近 3 轮窗口极差 <0.2 增长枯竭 / 同问题两轮无改善 stalled / 墙钟动态预算，见 §14-v15.5-A/B。~~主判据是动态的（θ_accept 达标收敛 / δ=0.3 边际早停 / 同问题两轮无改善 stalled）；同时按档位设收敛契约 maxIter（fast 3 / standard 5 / deep 6），max_rounds=8 为全局防呆值~~）

### 4.1 参数动态调整（maintainer 提出；v15.4 修订：budgetCap 已删除）

> **v15.4 修订**：`budgetCap`（单次 run 全程累计成本上限）**已随 v15.4 删除**——预算只做选择参考与余额感知报告，不做用量控制。本节保留历史记录，budgetCap 相关行失效；θ/λ 的动态校准逻辑保留（λ 语义按 v15.4 温和化）。

三个参数统一哲学：**静态基线 + 运行期校准 + 用户覆盖**。

| 参数 | 基线 | 第一版动态修正 | 第二版（数据驱动） |
|---|---|---|---|
| ~~budgetCap~~（v15.4 已删） | ~~档位默认值~~ | ~~①预检校准 ②余额联动 ③--budget 覆盖~~ | ~~历史成本均值回归~~ |
| θ_accept | 档位默认值 | ①任务类型校准：用同类型任务「成功收敛时 S_r 中位数」微调（code 类偏高、research 类偏低——事实验证严格天然压分，固定 θ 会让 research 永不到标）②`--threshold` 覆盖 | ~~收敛分布锚定：θ 不随 Verifier 松紧漂移，锚定硬门禁（客观），目标=80% 任务 2 轮内收敛/10% 早停/10% 到上限~~ **（v15.5 失效：θ 统一 9.5 为理论天花板，多数 run 以 early_stop 收尾；θ 归结构级参数，改动走 §12.5 结构级流程 + maintainer 终审，不再自动校准）** |
| λ | 档位默认值 | ①~~预算使用率连续因子~~（预算已删）②全局余额紧张因子 ③`--lambda` 覆盖 | 高价溢价学习：高价模型 verifier 分不高于低价模型 → λ 自动上调 |

**四条铁律**（防参数跑飞）：①所有调整必须有数据依据 + 写进 decisions.jsonl（「为什么是这个值」可审计）；②每个参数有 clamp 范围（min/max 带内调整）；③至少 10 条有效 council 的数据才允许改变基线（防小样本噪声）；④用户覆盖永远最高优先级。

## 5. 选择策略（Selection Strategy）

**候选空间是 `model@thinking` 档位**（D4）：评分函数在档位粒度上打分，自动权衡「高 thinking 的质量提升 vs 成本与延迟上升」——某个模型 @high 太慢太贵时，评分函数自然落到 @medium 或 @off。

```
score(候选m@t, 子任务t) = Σ_d w_d(t) × cap(m@t, d) − λ × effectiveCostNorm(m@t)
                         − μ(时间压力) × latencyNorm(m@t) + div(m)

effectiveCostNorm(m@t) = 归一化( unitCost(m@t) × scarcity(m, 余额快照) )
```

- `w_d(t)`：子任务维度权重（Decomposer 输出）
- `cap(m@t, d)`：该档位的能力档案分数
- `unitCost(m@t)`：静态单位调用成本（见 §7，含 thinking tokens）
- `scarcity(m, 余额快照)`：**余额/额度稀缺因子**（见 §7）——余额吃紧时抬高有效成本，逼 selector 转向其他模型
- `latencyNorm(m@t)` + `μ`：延迟惩罚项——高 thinking 档位 P50 延迟大；普通任务 μ 默认较小，长任务/临近超时风险时 μ 上调
- `λ`：档位基准值（v15.4 温和化：能力分主导、成本微调、同分 tie-break，惩罚封顶大幅调低，deep 档 λ≈0）；`scarcity` 已承担「余额」维度（~~预算使用率 >80% 时 ×1.5~~ 预算已删，v15.4）
- `div(m)`：厂商多样性加分——本 council 已用厂商之外的新厂商 +0.3

**六道硬护栏**（违反直接过滤，不参与评分；冲突时按优先级只报最高者，实际实现 `selector.passes_guards`）：

| 优先级 | 护栏（reason_code） | 触发条件（阈值） | 实测值 |
|---|---|---|---|
| 1 | identity_unknown | 身份未知（无可靠 API 路由）一律剔除 | identityUnknown=true |
| 2 | unstable_member_excluded_from_pool | stable=false（临时成员不参选）（**v15.5 修订：stable 职责并入池名单——不在 `model-pool.json` 的成员一律不参选，此护栏由池名单准入替代**，见 §14-v15.5-K） | stable=false |
| 3 | thinking_not_allowed | thinking 档位不在该模型档位表 | 实际档位 vs 档位表 |
| 4 | circuit_open / circuit_half_open_probing | 熔断开启（冷却中）；半开探测期只放行 1 个探测 | 熔断状态 |
| 5 | balance_exhausted | 余额/额度不足以一次调用（quotaFactor=∞） | quotaFactor/余额快照 |
| 6 | self_verify_ban | Verifier 与执行者 **baseModel 相同**（thinking 档位不改变固有偏见，@off 执行 + @medium 自验依然算自验） | baseModel ∈ bannedModels |

- **可用性/充足性**（原护栏 1/4/5 语义）：候选空间即「档案中存在 + stable + 有 API 路由」的条目，充足性预检保证 run 至少 2 家可用（见 §5.1）；生态多样性由评分函数的厂商多样性加分（div=+0.3）软性保证。
- **护栏事件日志（v15，评审报告 H 项）**：每次剔除写 `guardrail-events.jsonl` 结构化事件——**ts / run_id / guard_name / candidate / threshold（阈值）/ measured（实测值）** 五要素齐全；控制台「护栏与告警」Tab 可查历史，`/metrics` 暴露 24h 命中计数，事后可复盘「哪道护栏、什么阈值、什么实测值、什么时刻触发」。

### 5.1 2-baseModel 常态模式（maintainer 拍板：现状就是 2 家，必须按第一公民设计；**v15.4 已升级为动态参与**）

**交叉执行 + 交叉验证**（比「执行/验证分家」更优，无验证缺口）：

```
子任务 1、3 → DeepSeek 执行 → MiniMax 验证
子任务 2、4 → MiniMax 执行 → DeepSeek 验证
```

- 自验禁令满足（谁都没验证自己的）；**所有子任务都被独立验证，无缺口**；执行多样性保住（两家都干活）。
- **实际分配语义（v15.4 修订，§14-v15.4-E；v15.5 再修订，§14-v15.5-C）**：~~每个子任务由 selector 独立选最优执行者（不强制轮替）~~ → **层次 1 执行者轮换**（每 baseModel 轮内最多执行 ⌈子任务数/可用 baseModel 数⌉ 个子任务，防全职化）；验证者数量动态化 ~~`k = clamp(异厂商 baseModel 数 − 1, 1, 3)`~~ → **v15.5：以 vendorGroup 厂商计**，verifier 之间必须厂商互斥（修复 v4-pro/v4-flash 同厂商互评漏洞）；standard/deep 双路执行互评（同轮不评自己 + 跨轮人人有份）；verifier 拿到全局上下文（广播语义）。
- **双验证者尺度校准**（关键配套）：两家验证者打分松紧不同，直接聚合 S_r 会失真——统一 rubric + 校准锚 + z-score 归一化后再聚合。
- **冷评审降级**：本轮两家都参与了，无独立第三方——冷评审**跨轮延迟**（下一次 council 顺手评审上一次报告）+ 双评审一致，指标标注「弱冷评审、独立性受限」。
- **deep 档照跑**：用更严门禁 + 更多轮数 + 降置信度标注补偿独立性的不足，而非拒绝运行。

**未来升级路径（v15.4 已拍板落地）**：~~≥3 家自动启用完整模式（执行组/验证组分离 + 本轮独立冷评审），两种模式都已实现，加模型即自动切换。~~ v15.4 E-17/20 已实现：验证者数量随可用 baseModel 数动态化（上限 3）+ 执行者轮换 + standard/deep 双路互评。

同 provider 并发限流（按 provider 不按档位）、超时 fallback 链、watchdog 等现有机制全部保留。**Fallback 语义（maintainer 拍板）**：主候选超时/失败后，①优先选**同一 baseModel 的更低 thinking 档**（@high→@low/@off，质量同源、更稳更快）；②降档仍失败 → 跨模型 fallback 链；③两次都失败 → 不再试，记入 ledger，子任务标注「缺失视角」，合成时降置信度。**Ox Alpha (Stealth) 进 fallback 链末端**（免费多一条命，但因每日调用次数极有限 + 不稳定，只放末端）；初期测试阶段即可把它纳入候选池验证。

### 5.2 模型故障熔断器（maintainer 提出：临时维护/不可用时的重选模型）

标准 circuit breaker 三态 + 指数退避，无需人工干预：

```
closed（正常）→ 窗口内连续 3 次失败 → open（冷却 5 分钟起，指数翻倍，上限 1 小时，不调度）
→ 冷却结束 → half-open（同一时刻只试探性用 1 次）→ 成功回 closed / 失败回 open 且退避翻倍
```

- 运行中子任务主模型失败 → fallback 链立即接手（Q4）+ 该模型进熔断（`selector.record_failure`，失败结算已接线）；
- 熔断状态持久化到 `circuit-state.json`（原子写）；与充足性预检联动——**熔断中的模型按「不可用」计，不数进下限**（不触发模式震荡）；
- **参数外置（v15.1）**：阈值/窗口/退避/探测 TTL 全部在 `council-params.json` 的 `circuit.*`（默认：连续 3 次失败 / 10 分钟窗口 / 退避 5min→1h / 半开探测 TTL 5min）；
- UI「模型与能力」与「护栏与告警」Tab 显示熔断状态（XX 疑似维护中）。

## 6. 终止策略（Termination Strategy）：动态轮数

> **v15.5 修订（2026-08-25 拍板，见 §14-v15.5-A）**：①θ 三档统一 **9.5**（原 7.0/8.0/8.5），语义重定位为「理论满分天花板」——verifier 极难给出 9.5+，多数 run 以 early_stop 收尾，converged 变稀有（预期内）；②「边际收益早停」判据从「相邻两轮 ΔS < δ」改为「**近 3 轮滑动窗口极差 < δ=0.2**」（防 ±0.3 震荡永不停）；③**删除全部轮数上限**（档位 maxIter 与全局 maxRounds=8 均不再触发 forced，maxRounds 实为死参数）；④防失控 = 增长枯竭 + 墙钟（动态预算，见 §6 下方 B 项）+ 硬门禁 stalled + 余额耗尽护栏。本节下文的 max_iter/max_rounds/δ=0.3 相关行失效。

每轮 r 结束，Verifier 产出三样东西：**整体分 S_r（0–10）**、**硬门禁结果**、**rework 清单**。

**硬门禁**（任一失败 → 必须返工，借鉴 Leige）：

- **事实断言强制检索验证**（maintainer 拍板）：所有数字/统计/市场规模/公司名/产品名/价格类断言必须**检索验证 + 标注 + 附链接**——数据错误是致命问题，容不得编造和杜撰。分级：公认常识豁免；推理结论不检索但标注「依据已验证断言推导」。任何未经验证的数字/事实断言 → 硬门禁失败。
- **验证三分类**：confirmed（多个独立来源一致，附链接）/ refuted（找到反证 → 必须返工）/ unverifiable（查不到可靠来源 → 强制降级标注为「估计/观点」，不得以事实口吻出现）。
- 结论无证据支撑的条数 > 阈值（如 3 条）
- 子任务间存在未裁决的关键矛盾
- 模型使用不可追溯（缺决策日志）

**检索验证执行架构**：reviewer 直连 API 无搜索工具，走「生成 → 验证」两阶段——reviewer 输出强制附「事实断言清单」（每条：内容 + 声称来源）→ orchestrator 自动检测无链接断言 → 逐一检索 → 三分类结果回写 reviewer 输出并进 runtime-feedback（成为能力档案「事实性」维度的自动数据源）。检索工具：Google CSE 免费额度 100 次/天优先，或 host-bridge 主会话 web_search 分工；council 低频使用，免费额度基本够。

**决策顺序**（每轮按序判断；**上限防呆对所有路径生效**，含硬门禁路径——v15.1 修复：此前硬门禁返工不检查轮数/预算，rework 清单每轮变化就会无限返工）：

1. 硬门禁失败 → 返工；**同一问题连续 2 轮无改善 → stalled**，停止循环，输出根因分析报告（Leige 的「两轮没改善停手查根因」）（~~硬门禁未消除时 `r ≥ max_iter` 同样 → forced~~ **v15.5 删除轮数上限**；~~成本 ≥ budgetCap~~ v15.4 删除成本 forced）。
2. `S_r ≥ θ_accept` → **收敛**，进入合成（**v15.5：θ=9.5 三档统一，实为理论天花板**）。
3. `近 3 轮窗口内 S_r 极差 < δ` → **增长枯竭**，停止返工，用当前最佳结果合成，报告标注 `confidence = f(S_r)`（**v15.5 重定义：原为相邻轮 ΔS < 0.3 早停；δ=0.2**）。
4. （~~`r ≥ max_rounds`（全局防呆值 8）→ 强制收敛~~ **v15.5 删除**——不设轮数上限，只追求最佳收敛）
5. 否则 → 返工：下一轮**只执行 rework 清单上的项**（谁补证据、哪个结论重审、期望产出），不重跑已达标部分。

初始参数：**v15.5 起 `δ = 0.2`、θ = 9.5 三档统一、无 max_rounds**（~~δ = 0.3、max_rounds = 8~~）。

**轮次产物协议**（借鉴 Leige 修订号）：所有输出文件带轮号，如 `reviewer1-r2-output.md`；rework 清单引用上一轮文件路径，保证证据链可追溯。

## 6.5 活性超时模型（替代固定总时长超时）

**问题**：固定总时长超时（现 120s）会掐断仍在正常推理/输出的模型，尤其高档位（DeepSeek `max`、MiniMax `high`）思考期长，慢 ≠ 挂掉。

**原则**：不设「总时长」上限，只设「**连续无输出的空闲时长**」上限——只要模型还在吐 token（含思考 token），就永远等它；只有真正卡死（一段时间没有任何字节到达）才判定超时。

**机制**（orchestrator 直连 API，天然可解，也印证 D1 选 Python orchestrator 的正确性）：

1. **流式接收**：`stream: true`，逐 chunk 读 SSE，不再 `resp.json()` 一次性等完整响应。
2. **活性心跳**：每到达一个 chunk 就刷新 idle 计时器。**思考阶段也算活性**——OpenAI 兼容接口的 `delta.reasoning_content`、Anthropic 兼容接口的 `thinking_delta` 都持续流动，所以「模型在思考」不会被误判空闲。
3. **idle 超时**：连续 `idle_timeout`（**180s**，与实现对齐）无任何字节到达 → 判定挂掉 → 触发 fallback（降档 → 跨模型）。
4. **总时长兜底**：保留一个极宽松的总上限（**v15.4 定为 900s**，~~原 1800s~~——防止一次卡死调用耗光 run 墙钟预算；**v15.5 起 run 墙钟改动态预算，见 §14-v15.5-B**）防僵尸连接，正常推理到不了，只是保险丝。

**附带收益**：流式改造顺带实测 **TTFT（首 token 延迟）与每秒 token 数**，直接喂给能力档案的 `latencyP50Ms`，让选择策略的 `μ·latencyNorm` 用真实数据而非估算；同时可给用户实时进度（「模型思考中，已产出 N token」）。

**实现改造点**：`call_openai_api` / `call_anropic_api` 改为流式（aiohttp 逐行读 `resp.content` 解析 SSE）；超时从「total 主判」改为「idle 主判 + total 兜底」。原 `call_anropic_api` 里 `thinking: disabled` 需按档位放开，由选择策略决定档位而非写死禁用。

## 6.6 Reviewer 受限只读工具集（function calling 循环）

**动机（maintainer 提出）**：reviewer 直连 API 无工具，读本地文件（代码审查、文档分析、前序产物）只能靠 orchestrator 把文件塞进 prompt——上下文爆炸 + 截断风险 + 笨重。**解法：给 reviewer 工具，orchestrator 做 function calling 循环**。

```
reviewer 直连 API + 工具声明 → 模型发起工具调用 → orchestrator 执行（只读+白名单）→ 结果回填 → 模型继续推理
```

- **工具集（只有读取类）**：`read_file`（白名单目录内）、`list_dir`、`search_content`——**没有写、没有执行**（Leige 的「执行 AI 有工具、验收 AI 只读」）。
- **安全边界**：路径白名单 = run 工作区 + 任务指定的审查目标目录；单文件 200KB 上限；目录深度上限。
- **附带解决两个悬而未决项**：长上下文任务（按需读代替整包塞 prompt，省 token）；子任务依赖（reviewer 用工具读前序产物，orchestrator 不拼接）。
- **与活性超时的兼容**：模型发起工具调用后等 orchestrator 执行期间无 token 输出——**工具执行耗时不计入 idle 计时**（工具在跑 = 活着；只有「连工具调用都没有的静默」才算卡死）。
- **成本核算**：工具读入的内容 token 计入输入成本，token 预估器按「白名单目录大小 × 预估读取比例」预估算。
- **tool_use 维度测试内容由此明确**：给被测模型一个测试沙箱目录（若干文件），考「读文件 → 综合 → 回答」，答案可从文件内容客观验证——客观判分，无需 judge。

## 7. 成本模型（成本函数：时间因子 × 额度因子）

核心抽象：**成本不是静态单价，而是「当前时刻 + 用量状态 + 输入规模」的函数**。统一为：

```
effectiveCost(m@t, now, usage, estInputTokens)
    = piecewiseUnitCost(m@t, estInputTokens) × timeFactor(now) × quotaFactor(usage)
```

- `piecewiseUnitCost`：分段单价（按 thinking 档位 + 输入 token 分段，见 7.7）
- `timeFactor(now)`：时间因子（峰谷价，见 7.3）
- `quotaFactor(usage)`：额度/余额因子（见 7.4）
- `estInputTokens`：**调用前预估的输入 token 数**（token 预估器，见 7.7）

### 7.1 定价档案（pricing profile）——按计费模式分类

每个 provider 声明自己的计费类型，未来加新 provider 只需加一条档案，不改 selector 代码：

| 类型 | 代表 | 边际成本 | 约束来源 |
|---|---|---|---|
| `pay_per_token` | DeepSeek 官方 | token 价 × 峰谷（真扣款） | 余额（钱） |
| `free_pool` | MiniMax Token Plan | 0（年费已付） | 5h/周窗口额度 |
| `subscription_multi_quota` | OpenCode Go | 0（月费已付） | 滚动 5h + 周 + 月三重限额 |
| `subscription_tiered_deferred` | GitHub Copilot | 额度内 0，超额按量价 | 免费额度 + 递延账单 |

**多币种与 credits 换算链**（maintainer 提出）：

- 每个价格带声明 `currency` 字段（CNY/USD/null），**内部统一记账货币 = CNY**；汇率由「每日定时任务」自动更新（见 7.1a），不再手动维护。
- **credits 类**（Mimo/Copilot：token → credits → 货币）：档案声明两步换算链 `tokenToCredits`（input/output 系数）× `creditsToCurrency`（每 credit 多少钱 + 币种），selector 按链折算成 CNY。
- **调用次数额度类**（OpenCode Go）：边际成本 0、不进预算计算；订阅均摊（会计成本）只进报告展示，不参与调度。
- 报告双轨：每笔调用记录「原始币种金额 + CNY 换算」，审计可追溯。

### 7.1a 汇率每日自动更新（maintainer 拍板：定时任务 + 记录当日汇率）

- **数据源（已实测可用）**：中国外汇交易中心 CFETS 官方中间价 `https://www.chinamoney.com.cn/r/cms/www/chinamoney/data/fx/ccpr.json`（免费无 key；需浏览器 User-Agent + Referer；`records[].vrtEName=="USD/CNY"` 取 `price`，2026-08-21 实测 6.7817；覆盖 24 货币对，未来加任何币种模型都够用）。**备用**：`https://open.er-api.com/v6/latest/USD`（市场价口径，北京 8:00 更新，无 key）。
- **更新时机**：官方中间价每天 **9:15 发布** → host-bridge 宿主插件（host-bridge plugin）**每日 09:30**（北京时间口径，`toISOString()` 是 UTC 时刻、必须显式 UTC+8 换算判断）轮询触发抓取，写 `< user-data-dir >/exchange-rates.json`（当前生效汇率 + 发布日期 + 来源），同时追加 `< user-data-dir >/exchange-rates-history.jsonl`（当日汇率历史，maintainer 要求记录）。
- **失败降级（v15.4 升级为三级 fallback）**：抓取失败 → 备用源 → 再失败沿用最近一次成功汇率 + 标记 `stale: true` + `staleReasons: ["fetch_failed"]`；绝不回退硬编码值。**v15.4 起 stale 不再停机拒绝运行**（只降级成本置信度 + `fx_warning` 告知主会话），见 §14-v15.4-A。
- **陈旧语义（v15，评审报告数据语义项）**：`stale` 按**交易日历**判断，不只依赖「抓取失败」——CFETS 周末/节假日不发布，抓到旧日期同样标 `stale`（`publishDate_outdated`）；预期发布日 = 工作日 9:15 前 → 上一工作日，9:15 后 → 当天（周末回退最近工作日；节假日未建模时保守标 stale——宁可提示，不隐藏旧价）；更新超过 26 小时 → `fx_rate_age>26h`。`staleReasons: string[]` 供 UI/指标/告警消费。
- **实现**：`orchestrator/fetch_exchange_rate.py`（独立可跑；宿主插件 60s 轮询 09:30 后触发，失败不记日期、下一轮自动重试）。

### 7.2 边际成本 vs 会计成本（决策视角 vs 报告视角）

- **决策用边际成本**：订阅费（$10/月、$39/月、年费）是**沉没成本**，已经花了，决策时不该被它绑架。订阅制模型在额度内「多调一次」的增量成本 = 0，成本约束完全体现在「额度稀缺」上；按量制（DeepSeek）的增量成本 = 真金白银。
- **报告两种都展示**：最终报告里同时给出「决策成本（边际）」和「会计成本（订阅费均摊）」，前者驱动调度、后者用于核对订阅是否划算。
- 这正是 Q3 的答案：Copilot 免费额度内边际 0、超额按 `overtimePrice` 计入成本预估；递延到下月账单只改变现金流时点，不改变成本总额，selector 正常计入即可。

### 7.3 时间因子（峰谷价，答 Q2）

- DeepSeek 官方：`timeFactor = 1.0（工作日 9-12/14-18 高峰）/ 0.5（其余+周末全天低谷）`，selector 按**当前调度时刻**取价。
- **（maintainer 裁定）不做时机调度**：council 不延迟任务等低谷、也不设「忙时避开 DeepSeek」的规则——峰谷价只是成本项的时间快照，若按当时成本权重计算 DeepSeek 仍然胜出（能力溢价值得），就选 DeepSeek；选择权完全在评分函数。

### 7.4 额度因子（答 Q1）

`quotaFactor = scarcity` 由「余额/额度快照」算出，规则：

- **多窗口取最紧**（OpenCode Go 滚动 5h + 周 + 月、MiniMax 5h + 周）：`scarcity = max(各窗口稀缺度)`，最短的板决定水位。
- **区分窗口性质**：
  - 速率限制型（滚动 5h）：耗尽立即限流但可恢复 → 软化，scarcity 中等（1.5 左右）
  - 总量配额型（周/月）：耗尽要等重置 → 硬化，scarcity 拉满（3.0）
- **分段**：充裕（>50%）→ 1.0；吃紧（<20%）→ 递增到 2~3；耗尽（不足一次调用）→ 硬剔除（护栏 3）。
- **阶梯递延**（Copilot）：免费额度内 0，超额跳到按量价——scarcity 在额度耗尽前后是阶跃而非渐变。

`quotaFactor` 的核心价值：**防止「性价比陷阱」**——免费/订阅模型边际成本为 0，评分函数会天然偏袒，但额度有限；`quotaFactor` 在额度快耗尽时抬高有效成本，逼 selector 平滑切换，而不是把免费池/限额一口气打爆。

### 7.5 余额查询自动化

- 查询时机：council 运行前一次 + 每轮调度前一次，60s 缓存（复用 host-bridge cost-monitor 的端点与解析逻辑；council 与模型调用共用同一把 API key，无跨系统凭证问题）。
- 结果写入 `runs/<ts>/budget-snapshot.jsonl`（记录每次调度时刻的余额/额度快照），保证审计可追溯。
- 查询失败时**保守处理**：按「吃紧」计（scarcity=2.0），宁可保守降权也不盲目乐观。

### 7.6 成本生效位置（v15.4 修订）

- 评分函数：`λ × effectiveCostNorm`（quotaFactor 已含余额/额度维度；v15.4 λ 温和化）
- 护栏：余额/额度红线（§5 护栏 5，硬剔除耗尽者）
- ~~终止判据：累计成本 ≥ budgetCap 强制收敛~~（**v15.4 删除**——预算不做终止）
- run 级实时成本追踪：每次 API 调用追加 `runs/<ts>/cost.jsonl`（记录 `model@thinking`、tokens、边际成本、会计成本、余额快照）
- 报告末尾保留「模型使用记录」表（现有硬性要求），按 `model@thinking` 逐档列出边际成本与会计成本。

### 7.7 输入分段计价 + token 预估器

**分段单价**：某些 provider 对「单次输入 token 数」分档计价（如 ≤32K 一个价、>32K 另一个价）。在定价档案里声明 `inputTiers`：

```json
"inputTiers": [
  {"upTo": 32000, "rate": 0.28, "mode": "marginal"},
  {"upTo": null,   "rate": 0.42, "mode": "marginal"}
]
```

- **两种语义**：`marginal`（累进——前段低阶价、超出部分高阶价，类比阶梯电价）；`bracket`（整体跳变——输入落在哪段整体按那段价）。
- 无分段的 provider 用单段 `[{"upTo": null, "rate": 基础价}]`，不影响既有逻辑。

**token 预估器**（分段计价引出的硬需求——selector 调用前就要算成本，必须先预估 token）：

1. **静态估算**：输入 = prompt + source pack + 系统提示的字符数 ÷ 系数。系数按语种：中文 ≈ 1.5–2 token/字、英文 ≈ 1 token/4 字符、代码 ≈ 1 token/3.5 字符；输出按任务类型经验值（如 reviewer 1200 字 ≈ 1600 token）。
2. **运行期校准**：cost.jsonl 记「预估 vs 实际」token，反推每模型的真实「字符→token」系数（tokenizer 各不同，必须实测）。保守策略：预估取上限（高估成本），避免「预估便宜实际贵」。

**反过来指导决策**：分段计价让成本参与「任务怎么拆/怎么喂」——若某模型输入超阈值变贵，一个超长输入的子任务会触发高价档，selector 有两个更优选择：**换无此阶梯的模型**，或**先把输入摘要压缩到阈值以内再喂**。

## 8. Run 状态机

```
planning → decomposed → [round_r: assigning → reviewing → verifying → deciding]
   → accepted ──────────────→ synthesizing → reported
   → rework（带清单）────────→ round_{r+1}
   → stalled（两轮无改善）───→ 根因报告
   → 增长枯竭 / 墙钟动态预算 → 按当前最佳合成（标注置信度）（~~max_rounds~~ v15.5 删除轮数上限；~~budget_cut~~ v15.4 已删）
```

每次 run 写：`decisions.jsonl`（调度 why 日志）、`cost.jsonl`、`rounds/rN-*.md`（各轮产物）、最终报告。ledger 增加字段：轮数、各轮 S_r、早停原因、动态分配 vs 护栏触发记录。

## 8.5 Council 双输出模式与 host-bridge 工具化（maintainer 提出：融入主会话的判断引擎）

### 8.5.1 双输出模式

| 模式 | 输出 | 用途 |
|---|---|---|
| **report**（现有） | 完整报告 + 模型使用记录 + 审计 | 重大决策，maintainer 审报告 |
| **inline**（新） | synthesizer 结论**直接作为主会话的回复内容**，不生成报告文件（ledger 轻量记录一条） | 对话中随时可用，用户无感——像主会话自己想出来的（类似 OpenRouter fusion） |

**inline 模式必须轻量化**（对话要流畅）：1 轮、2-3 个模型、fast 档参数、flash 档模型、无完整报告——目标 **30-60 秒内**返回结论。

### 8.5.2 触发与 host-bridge 工具化

- **`run_council` 工具**（host-bridge 注册）：输入 = 任务 + 模式（report/inline）+ 档位 → 后台跑 orchestrator → inline 返回结论文本给 agent、report 返回报告路径。
- **触发规则**（写进 host-bridge 版 model-council skill）：主会话 agent 自判「此问题值得 council」→ 调 inline；或用户显式要求「用 council 想想」。**自动触发正反例清单**（skill 内写明）：
  - ✅ 正例（自动触发）：**设计新插件/新功能**（脑图、知识库这类）、重要架构决策与技术选型、高风险操作前（删数据/大迁移）、花钱/不可逆决策；
  - ❌ 反例（不触发）：日常问答、明确的小改动、已定型的执行、纯翻译/格式调整；
  - 每次自动触发记录「为什么触发」（触发日志），maintainer 事后可 review、规则可迭代；用户覆盖永远最高优先级。
- **「设计新插件」场景的两种用法**：设计前（council 出方向：架构选型/宿主组合 vs agent 预设/插件结构/风险点，主会话在骨架上细化）；初稿后（council 多视角挑坑，类似 code review）。
- **融入所有重要判断**：对话中的重要问题、写代码的关键架构决策、重要文件操作前、风险操作前——主会话都能像调自己的深度思考一样调 council。
- **inline 模式的成本纪律（v15.4 修订）**：fast 档参数 + 轻量模型；单次 inline 成本约 ¥0.01 级（~~λ 和 budgetCap 天然约束~~ 预算已删，靠 λ 温和项 + 余额护栏 + 实际成本事后对账观察）。

## 9. 文件与工具清单（实际落地，v15.1）

**数据文件（`< user-data-dir >/`）**：
- `capabilities.json` — 能力档案（唯一决策数据源，`model__thinking` 粒度，revision 单调递增 + `.rev<N>.json.bak` 可回滚）
- **`model-pool.json` — 模型池成员名单（v15.5 新增：model 粒度，成员准入第一层；active / retired-by-user / retired-by-host-bridge-removal 三态）**
- **`model-tier-bridge.json` — host-bridge 模型档位桥（v15.5 新增：插件从 `ctx.llm.listModels/resolveModelInfo` 生成；档位枚举 + wire 拼写 + maxTokens 单一数据源）**
- `council-params.json` — 可调参数外置（tiers/circuit/selection/terminator/streaming/feedback/ingest/fx/judgeDrift/sla）
- `pricing-profiles.json` / `pricing.json` — 定价档案（峰谷价/thinking 系数/额度窗口）
- `exchange-rates.json` + `exchange-rates-history.jsonl` — 汇率与每日历史（CFETS）
- `balance-snapshot.json` — 余额/额度快照（60s 缓存；单位契约：整数百分比 96=96%）
- `circuit-state.json` — 熔断状态；`guardrail-events.jsonl` — 护栏事件（五要素）
- `judge-drift.json` + `judge-drift-events.jsonl` — judge 漂移与告警
- `sla-report.json` — 各档 p50/p95 实测 vs 目标；`cost-drift.json` — 7 日成本对账
- `failed_runs.log` — 宿主 subprocess 失败审计（退出码/超时）
- `evals/runtime-feedback.jsonl` — 运行期反馈环（§3.5）
- `benchmark/scores/` + `scores-summary.json` + `regression-baseline.json` + `pending-ingest-diff.json` — 基准数据与门禁
- `runs/<ts>/` — decisions.jsonl / cost.jsonl / rounds.jsonl（含 termination_audit）/ result.json / report.md / claims-r*-*.json / rework-r*.json

**Orchestrator（`orchestrator/*.py`）**：`council_v14.py`（主编排）、`selector.py`（评分+六道护栏+熔断）、`terminator.py`（收敛判据）、`budget.py`（预算预检）、`calibration.py`（双验证者尺度校准）、`stream_llm.py`（流式+活性超时）、`verify_claims.py`（事实断言清单）、`update_capabilities.py`（反馈融合）、`cost_calibrate.py`（成本对账）、`query_balance.py`（余额查询）、`fetch_exchange_rate.py`（汇率）、`judge_drift.py`（金标漂移）、`sla_stats.py`（SLA 统计）、`dry_run.py`（历史回放）、`params.py`（参数加载）、`file_lock.py`（文件锁）、`caps_guard.py`（写前校验）、`config_loader.py`（凭证/时区/档位映射）+ 10 个测试文件（`*_test.py`，pytest 全绿）。

**Benchmark（`benchmark/`）**：`build_capabilities.py`（档案生成）、`capability_ingest.py`（diff/apply 审批摄入）、`regression_gate.py`（回归门禁）、`golden/golden-set.json`（judge 金标集）、`v2/`（配对比较/BT/校准方法论套件）、`archive/`（历史归档：2026-08-24、**2026-08-25-pre-alignment（协议对齐前旧成绩，v15.5-J-26）**）。

**治理文档（`< user-data-dir >/`，v15.5 新增）**：`meta-review-2026-08-25.md`（元评审 22 条问题清单）、`v15.5-roadmap.md`（Phase 0-A/B/C → 5 实施路线）、`tier-alignment-plan.md`（协议对齐方案细节）、`ui-model-pool-brief.md`（模型池管理前端设计方案）。

**host-bridge 插件**：`host-bridge plugin`（host `index.js` + 浏览器 `client.js`）——工具 `run_council`/`council_status`；HTTP `/api/council/state|settings|run|guards` + `/metrics`（Prometheus）；侧边栏卡片 + 设置页六 Tab 控制台；每日定时：09:30 汇率（v15.4 三级 fallback）+ 04:00 成本对账 + 04:30 judge 漂移（v15.4 后接自动 apply + 金标 evolve）+ 02:00 换题检测（v15.4 新增）。

## 10. 实施路线（每阶段可独立验证、可回滚）

### 阶段 0 详细定义：最小可信基准 v2.1

现有 benchmark 不可信（§3 审查结论），阶段 0 从「清洗旧数据」改为「跑一个可信的最小基准」。设计如下：

**模型池**（手头全部可用模型 × 代表档位，约 6–9 个候选条目；**v15.5 修订：插值已废除——全档位×全案例，此表为历史记录**）：

| 底层模型 | 代表档位（全量测） | 其余档位 |
|---|---|---|
| deepseek-v4-pro | off / high / max | ~~low 插值~~（v15.5 起全档实测） |
| deepseek-v4-flash | off / high / max | ~~low 插值~~（v15.5 起全档实测） |
| MiniMax-M3 | off / medium / high | ~~minimal / low 插值~~（v15.5 起全档实测） |

**判分设计**（解决「judge 不能自评」——手头只有 DeepSeek/MiniMax 两家，judge 与被评者必然重叠，故分两类）：

1. **客观题 60%，零 judge 自动判分**：数学/逻辑（有标准答案）、代码（真实跑测试）、格式遵循（脚本解析 JSON / 数字数 / 查禁用词）。无 judge 偏差。
2. **主观题 40%，交叉评**：中文写作、长文分析——**DeepSeek 评 MiniMax 的响应、MiniMax 评 DeepSeek 的响应**，judge 与被评者永远互斥，谁都不评自己。
3. **research 类案例评测标准随之改变**（强制检索验证政策下）：不再评「记住了多少」，改评「检索验证质量」——给被测模型检索能力，按「断言覆盖率 / confirmed 率 / refuted 处理正确性 / 链接完整性」打分。

**规模与成本**：**30–36 题**（maintainer 拍板 9 维全测：6 主维各 4 题 + 3 补充维各 2–3 题）；总调用约 300–400 次；成本约 $0.2–0.4。**（v15.4 修订：题数改为动态达标线——每维 95% CI ≤ ±1 分 AND 分布健康，实测 code 需 9 题/chinese 10/research 9/reasoning 7/…见 §14-v15.4-G-35，不再固定 30-36）**

**评分协议（修掉旧 benchmark 的全部 bug）**：带 rubric 提示词、不截断响应、**0 分归因机制**（见下）、异常重试而非记 5 分、记录 judge_response、bootstrap 置信区间、**分差 < 0.5 视为并列**（不再拿 0.07 分差排座次）、**温度 0 + 固定 seed（幂等，保证断点续传可比性）**。

**0 分归因机制（maintainer 提出：跑分无人盯看，必须自动分辨「真实 0 分」vs「异常 0 分」）**：

1. **机器硬信号自动分类**：响应缺失/空文件/`[ERROR]`/`[REFUSED]` 开头 → 异常，不记分标 `failed` 续传重试；**生成时 `finish_reason=="length"`（截断）在源头拦截**，加大 max_tokens 重试后才进判分；judge API 异常/解析失败 → 该题评分重试；judge 给 0 分但 rationale 空/无 evidence → 标 `suspect` 进复核；judge 0 分+有理由+引用原文 → 真实 0 分；明确拒答 → 记 0 分标 `refusal` 类型。
2. **suspect 自动复核**：交叉评对方重评该题——两次都 0 分且有理由=确认；复核分>0=取复核分；复核也异常=标 `give-up`（剔除但记录）。
3. **报告透明清单**：专设「0 分与剔除项清单」——每个真实 0 分/技术失败/give-up 附题目、模型、原因、重试次数、最终处理、响应文件路径，事后可完整核查。
4. **统计口径**：真实 0 分参与统计；异常类不进统计；每个模型标注「有效样本数/总题数」。

**断点续传（maintainer 要求，修正旧 bug）**：旧 `v6_gen_opus48.py` 把 `[ERROR]` 失败文件也当成「已完成」跳过——失败被静默掩盖。新协议：①产物即进度——每「模型×案例」生成/评分写独立文件（原子写：临时文件+rename），完成一道立即落盘；②**失败 ≠ 完成**——失败写 `failed` 标记（带原因），续传时重试，超上限标 `give-up`；③`--resume` 默认开（待办=缺失+failed）、`--fresh` 强制全量、`--only-failed` 只补败；④评分阶段同样逐题续传；⑤进程被杀最多损失正在跑的一道。

**增量注册协议（未来加模型，v15.5 修订）**：~~新模型跑同一套案例集 → 与现有模型 pairwise 交叉评校准 → 并入能力档案，不重跑全量~~ → **v15.5：模型池管理（§14-v15.5-K）——控制台手动加入（全档位）→ 手动跑分（全档位×全案例，断点续传）→ 成绩 ingest 入档案；与 host-bridge 目录自动同步删除**。

| 阶段 | 内容 | 验证方式 | 状态 |
|---|---|---|---|
| 0 | **数据目录迁移** `< user-data-dir >/`（一次性脚本导旧 benchmark 结果/ledger/evals，原目录留档）+ 跑最小可信基准 v2.1（见上）→ 生成 capabilities.json | 交叉评一致性检查；客观题判分脚本可复现；分差<0.5 正确标并列 | ✅ 完成（372 case 已摄入） |
| 1 | selector.py + dry-run 对比 | 10 个历史任务回放：动态分配 vs 旧静态表，看一致率与差异合理性 | ✅ 完成 |
| 2 | 收敛循环 + terminator.py + 数据格式定型 | 用 3 个真实任务跑通，验证返工真的只补清单项、早停真的触发 | ✅ 完成 |
| 3 | 反馈闭环 + 余额/额度核算 + 汇率定时任务上线 | §3.5 runtime-feedback.jsonl 自动收集 → update_capabilities 重算；query_balance 余额快照；每日 09:30 汇率更新；每次 council 后档案自动更新一次，revision 可回滚 | ✅ 完成（v15 + v15.1 加固） |
| 4 | 文档同步（SKILL/policy/WORKFLOW 指向新目录） | 按文档跑一次完整 council 无卡点 | ✅ 完成（本稿同步） |
| 5 | **Council 控制台 UI**（§13） | 六 Tab 全部可用：增删改模型/定价表单写回后，下次 run 生效（**v15.5：模型池管理（增=手动下拉、删=手动/host-bridge 自动同步、手动跑分）为 Phase 0-C 待实施，见 §14-v15.5-K）** | ✅ 完成（host-bridge plugin 插件） |

## 11. 风险与回滚

| 风险 | 对策 |
|---|---|
| 完全动态化失控（评分函数选错模型） | 四道护栏 + decisions.jsonl 全审计；`--static` 开关一键退回表驱动 |
| benchmark 数据过期 | freshness 衰减机制 + 模型换新触发重跑 |
| 收敛循环拉高成本/时间 | （v15.5）增长枯竭早停（3 轮窗口极差 <0.2）+ 硬门禁 stalled + 余额耗尽护栏 + 墙钟动态预算防异常；θ=9.5 理论天花板，多数 run 以早停收尾（~~v15.4 maxIter + max_rounds=8 防呆~~ v15.5 已删轮数上限）（~~budgetCap 硬上限~~ 已删） |
| 动态评分与人工直觉冲突 | 上线首月并行记录「动态分配 vs 旧静态表」对比，攒数据再调整权重 |

## 12. 参数裁定记录（2026-08 全部拍板）

1. ✅ 已拍板（随 Q10 改革配套）：档位参数初值——fast ¥0.03/θ7.0/λ1.0；standard ¥0.15/θ8.0/λ0.5；deep ¥0.35/θ8.5/λ0.3；全局 δ=0.3、max_rounds=8（仅防呆）；记账货币 CNY，汇率每日 9:30 更新。**（v15.4 修订：档位预算已删、λ 温和化重设、墙钟统一 1800s、汇率改三级 fallback，见 §14-v15.4；v15.5 修订：θ 三档统一 9.5、δ=0.2、删除 max_rounds 与轮数上限，见 §14-v15.5-A）**
2. 维度集：✅ 已拍板——6 主维 + tool_use/creativity/safety 三补充维**全部实施不分批**；30–36 题（6 主维各 4 题 + 3 补充维各 2–3 题）。**（v15.4 修订：题数改动态达标线，见 §14-v15.4-G）**
3. ✅ 已拍板：两道硬护栏——自验禁令（按 baseModel）+ ≥2 provider；配套「2-baseModel 常态模式」（交叉执行+交叉验证+尺度校准+弱冷评审）+ 护栏 5 充足性预检（1 家硬拒/2 家常态/≥3 自动升级）。**（v15.5 修订：验证者数量与互斥以 vendorGroup 厂商计——k=clamp(厂商数−1,1,3)、verifier 之间厂商互斥，见 §14-v15.5-C）**
4. ✅ 已拍板：fallback 降档优先（@high→@low）→ 跨模型 → 两次即止；Ox Alpha 进 fallback 链末端。
5. ✅ 已拍板（2026-08）：最小可信基准 v2.1 全 9 项——模型池（Ox Alpha 不进基准）、30-36 题 9 维、三类判分、评分协议（含幂等）、**0 分归因机制**、**断点续传**、成本约 $0.2-0.4、增量注册协议、题目清单开工前过目。
6. ✅ 已拍板（2026-08）：阶段 0 放行——第一步先出「题目清单 + 基准运行器设计」给 maintainer 过目，审完再写代码；与 Q7-Q10 裁定并行。
7. ✅ 已拍板（2026-08）：idle_timeout 180s + 总兜底 ~~1800s~~（v15.4 起单调用 total 900s，见 §6.5）+ 超时日志三件套（最后字节时间/阶段/已产出 token 数）。
8. ✅ 已拍板（2026-08）：成本函数七项全部确认；「闲时套利」裁定为**不做时机调度**——council 不延迟等低谷、不设忙时避开规则，成本按当时价格计算，选择完全由评分函数决定（DeepSeek 高峰仍胜出则照用）。
9. ✅ 已拍板（2026-08）：运行期反馈闭环五环节全接受；样本权重拐点 = 20（约 5-10 次 council 积累，防小样本带偏）；翻排名门槛 = 10 条有效 council（沿用 policy.md，数据量足以区分 0.5 分以上真实差距）。
10. ✅ 已拍板（2026-08）：接受改革——7 档 → 3 档语义（fast/standard/deep）+ 连续参数覆盖（--budget/--threshold/--lambda）+ 简单风险检查（不可逆/花钱/安全隐私→deep；重要→standard；日常→fast）+ 用户口头覆盖；任务类型交给 weightVector；旧 9 维 router 退役。inline 模式天然用 fast 档、report 用 standard/deep。**（v15.4 修订：--budget 覆盖已随预算删除；档位语义收敛为复杂度刻度 θ/maxIter/maxSubtasks；v15.5 再修订：θ 三档统一 9.5、maxIter 删除——档位 = 并行规模（maxSubtasks + 双路执行 + 验证者数），质量线不分档）**

## 12.5 Council 自改进治理（maintainer 提出：council 与 benchmark 应由 council 自己进化）

分层进化，各层授权不同：

| 层 | 内容 | 进化方式 |
|---|---|---|
| 参数/数据级 | 能力分、~~budgetCap~~（已删）、λ、汇率、δ/窗口等判据数值的带内微调 | 全自动（§3.5 反馈闭环 + §4.1 四铁律）；**（v15.5 修订：θ 从本层移除，归结构级——收敛判据的基线值改动必须 maintainer 终审，本次 9.5 即拍板；δ/窗口等判据数值的带内微调仍参数级）** |
| 题目级 | benchmark 题目失效/加难/新维度 | **全自动（v15.4 拍板，§14-v15.4-F）**：AI 出题 → 异厂商双盲复核+仲裁 → 金标池 → 晋升考卷池 → 区分度衰减退役；安全网=写前校验+体检+回归门禁+熔断（~~原：半自动天花板检测 + 你拍板~~） |
| 结构级 | 评分函数形式、护栏、收敛判据、维度集 | 提案 → 独立评审 → 试运行 A/B → **双门槛采纳（数据证明变好 + 你终审）** |

**结构级流程**：触发（数据恶化自动标记 / 新模型超现最高 0.5+ 分 / 你发起）→ council 出提案（附理由+预期效果+回滚方案）→ **未参与提案生成的模型独立评审**（2 家则交叉：A 提 B 审，沿用自验禁令思想）→ 新旧版跑同批历史任务 A/B → 数据门槛 + 你拍板。

**自指风险两道防线**：①独立评审者（提案者≠评审者）；②结构改动绝不自动（防 council 自我放水——放宽自己的门禁、偏爱某模型）。参数级可自动，结构级必须你终审。

**新模型加入的自进化路径**：增量基准 → 若显著更强 → 自动评估 judge/verifier 角色重分配（动态化已覆盖）+ 天花板检测触发题目加难。**（v15.5 细化：judge 角色重分配改走「评卷考场 + judge 档案 + 任期制换任交接」，见 §14-v15.5-E；执行者/verifier 走厂商出线 + challenger，见 §14-v15.5-G）**

## 13. Council 控制台 UI（四决策已拍板）

**已拍板**：① UI 位置 = host-bridge 固化插件；② 不分阶段、一次性全搞定；③ 余额自查 API（不耦合 cost-monitor）；④ 数据目录迁 `< user-data-dir >/`。

### 13.1 六个 Tab（实际落地）

| Tab | 内容 |
|---|---|
| ① 总览 Dashboard | 最近 run 摘要（任务/档位/成本/耗时/轮数/S_r/置信度）、S_r 收敛曲线、模型使用统计、余额/额度卡片 + 当前 scarcity 因子 + **汇率（含发布日期/stale 标记）** + **熔断状态（XX 疑似维护中）** + **档案 revision/最后更新时间/新鲜度徽章** |
| ② 模型与能力 | 模型池列表（baseModel+档位，**临时模型徽章：stable:false / identityUnknown**；**v15.5-K：池名单管理——＋增加模型下拉（host-bridge 目录）、手动跑分按钮、手动删除、host-bridge 删模型自动同步消失、未测量状态**）；每模型雷达图、成本、延迟（TTFT/tps）、可靠性（成功率/fallback 率）、样本数；启用/禁用（stable 开关） |
| ③ 定价与成本 | 定价档案列表（四类计费模式）；编辑表单（输入/输出 × 高峰/低谷）；汇率状态展示 |
| ④ 运行记录 | 历史 run 列表；run 详情（每轮谁干什么、rework 清单、S_r 轨迹、报告路径） |
| ⑤ 自改进 | 档案 revision/最后更新/运行期反馈 run 数/最近融合来源 run_ids/benchmark 摄入累计；结构级改动治理说明 |
| ⑥ 护栏与告警（v15.1 新增） | 24h 护栏命中汇总（**v15.5 Phase 0.3 起拆「新增/存量」两列 + 7 日趋势**，防存量群发误读为持续触发） + **护栏事件历史表（时间/护栏/候选/run_id/阈值→实测值）**、judge 漂移（基线→当前+告警）、预算 SLA 达标表（v15.4：成本降为观察、墙钟保留）、~~待审批 benchmark 摄入 diff~~（v15.4：自动合入记录 + 自进化熔断状态）、failed_runs 计数 |

### 13.2 架构

- `host-bridge plugin` 固化插件（`index.js` host + `client.js` 浏览器端，遵守固化插件规范：`__ModuleLoader__` 注册、inject 覆盖所有 ctx 访问）。
- host：webServer 挂 `/api/council/*`（遵守 `/api` 子前缀契约）；只做 `< user-data-dir >/*.json` 读写（读 5–10s 缓存、写原子替换），**文件是唯一数据源，不建数据库**；余额端点自查（60s 缓存）；`/metrics` Prometheus 指标（revision/drift/护栏命中/反馈环/汇率 stale/judge 漂移/SLA 违规/待审批 case）。
- client：React（`React.createElement`，无 JSX）；图表 **SVG 手绘**（雷达/折线），不引重型库；用 host-bridge theme tokens 保持视觉一致。
- 写操作：用户自操作不需审批流，改定价/标记稳定成员加确认弹窗。

### 13.3 设计质量契约（impeccable 流程，maintainer 拍板「做的好看点」）

- **模式 = Operate**（管理型控制台）：可扫读性 > 表现力——信息密度克制、视觉层次分明、一致性第一；品牌在精确细节（数字对齐、状态色语义、空态文案）。
- **视觉一致**：与 host-bridge 现有面板（看板/任务面板/cost-monitor）同一观感——theme tokens、间距节奏、卡片语言全部对齐，不能像外来页。
- **开发流程（阶段 5 开工时）**：
  1. `shape`：UX 规划（每个 Tab 的任务流、空状态、错误态）→ 视觉方案给 maintainer 过目后才写代码；
  2. `craft` 完整实现（5 Tab 一次做完，不分期）；
  3. 一轮 bounded 验证：桌面+窄屏各截一轮 → 缺陷一次批量修复 → 至多再确认一轮即停（不无限自检）；
  4. 交付前 `critique`/`audit` 各一遍（UX 启发式 + a11y/性能/响应式）。
- **硬性要求**：空状态（还没有基准数据/还没有 run 时）、加载态、错误态（文件读不到/API 挂）都必须有设计，不许裸奔；所有表格数字对齐、状态色只用于状态。

## 14. 评审修复记录（2026-08-24 council 评审报告 → 落地映射）

评审报告（`runs/2026-08-24_12-44-44/report.md`，结论置信度 0.75）指出「架构合理、实现成熟度不足」。全部问题已按此表落地：

### v15（报告生成当日落地，H=高 M=中 L=低优先级编号）

| 编号 | 报告问题 | 落地实现 |
|---|---|---|
| H1 | 自进化闭环断裂（revision=0，168 runs 未回写档案） | `council_v14.py` 每轮验证后写 `evals/runtime-feedback.jsonl`，收尾调用 `update_capabilities.update()`：贝叶斯融合 + revision 单调递增 + 旧版备份可回滚 + 结果写回 result.json（宿主工具可见） |
| H2 | benchmark 与档案脱节（scores→capabilities 最后一跳断裂） | `benchmark/capability_ingest.py`：`benchmark/scores/<cid>/*.json` → 按维度 EMA 并入档案 + `_source_run_ids` 可回溯 + 幂等（已摄入 372 case，revision 0→1） |
| H3 | 单位/时区/交易日历语义缺失 | `selector.norm_percent`（96=96%，0.96 自动 ×100）；`config_loader.SHANGHAI_TZ` 全系统唯一时间口径；`fetch_exchange_rate` 按 CFETS 交易日历判 stale（9:15 发布、周末回退、26h 上限，`staleReasons: string[]`）；CostContext 全字段带单位后缀；回归测试固化（`test_stale_reasons.py`/`test_cost_calibrate.py`/`selector_test.py`） |
| H4 | 护栏触发黑盒、收敛元数据未落盘 | `guardrail-events.jsonl` 结构化事件（ts/run_id/guard/阈值/实测值）；rounds.jsonl 写 `termination_audit`（action/reason/轮数/cost/预算/S_r 轨迹/调用数）；控制台可查历史（Tab ⑥ + `/api/council/guards`） |
| M1 | 可观测性缺失 | 宿主 `/metrics` Prometheus 端点（revision/drift/护栏命中/反馈环/汇率 stale；v15.1 加 judge 漂移/SLA 违规/待审批 case） |
| M2 | 陈旧数据语义缺失 | `computeStaleness()`：配额快照>90s / 汇率>26h / 档案>7d 三类 stale + reasons 数组，council_status 与 UI 展示 |
| M3 | run_council 目录名字符串排序静默回退旧报告；shell.run 不查退出码 | 宿主按 **mtime** 取最新 run（`recentRunDirs`）；无 result.json 显式报错（不再静默回退）；python 侧全部退出路径写 result.json + 失败退出码非 0；宿主 execFile 校验退出码/超时并拒绝成功；`dry_run.py`/`cost_calibrate.py` 同口径 mtime 排序 |
| M4 | 熔断器未接线 | `selector.record_success/record_failure` 在所有调用点结算；三态 + 指数退避 + 半开单探测；`circuit-state.json` 持久化 |
| L1 | 档位参数硬编码 | 档位化 maxIter（fast 3/standard 5/deep 6）+ tieBreak=cost_then_latency + 强制终止审计 |
| L2 | 缺项回退 | 维度缺失回退模型历史均值 + `fallback` 标记 + 样本不足 shrinkage 收缩向均值 |
| L3 | 修复回归风险 | `benchmark/regression_gate.py` 回归门禁（最新批 vs 基线 mean−1σ，CI 红灯）+ 全部修复带测试 |

### v15.1（本次收尾，逐项对齐报告「建议与下一步」表）

| 报告建议 | 落地实现 |
|---|---|
| 高：profile_updater 文件锁+原子写+写前校验+source_run_id+失败告警 | `orchestrator/file_lock.py`（O_EXCL 锁文件 + 死锁过期抢占 + 超时抛错）；`caps_guard.py` 写前校验（score∈[0,10]、revision 单调、维度契约）；`update_capabilities`/`capability_ingest` 全部锁内原子写；`runtimeFeedback.sourceRunIds` 记录融合来源 run_id；更新失败写 rounds.jsonl **且** result.json（宿主 run_council 输出显式告警）；无有效反馈 skipped（不空转 revision）；最小改动阈值修正（\|Δ\|<0.5 只记样本不写分，≥10 有效 run 才允许 ≥0.5 改动） |
| 高：run 判定 + 退出码（已在 v15-M3 完成，v15.1 补审计） | `failed_runs.log` 记录每次 subprocess 非零退出/超时（脚本/参数/错误/时间），council_status 与控制台展示 |
| 高：数据正确性回归测试 | `test_stale_reasons`/`test_cost_calibrate`/`test_ingest`/`test_regression_gate` fixture 修复（pytest 全绿 39 项）+ `test_update_capabilities`/`test_params`/`test_judge_drift` 新增 |
| 高：护栏结构化事件 + 控制台可查历史 | v15-H4 基础上升级为五要素（run_id/阈值/实测值补全）+ Tab ⑥ 事件历史表 + `/api/council/guards` 查询端点 |
| 中：选择权重、熔断参数硬编码 | `council-params.json` + `orchestrator/params.py`（深合并默认值 + 坏文件兜底 + `COUNCIL_PARAMS_FILE` 测试隔离）；tiers/circuit/selection/terminator/streaming/feedback/ingest/fx 全部外置，改动走文件评审 |
| 中：judge 漂移未监控 | `benchmark/golden/golden-set.json`（8 题固定答案+rubric）+ `orchestrator/judge_drift.py`（每日自评，\|漂移\|>1.0 告警落 `judge-drift-events.jsonl` + 退出码 2）；宿主每日 04:30 定时（`judgeDrift.enabled` 可关）；`/metrics` judge_drift 指标 |
| 中：预算 SLA 验证 | `council-params.json` 的 `sla.*`（§4 表）+ `orchestrator/sla_stats.py`（实测 p50/p95 墙钟与成本 → `sla-report.json`）；控制台达标表 + `/metrics` 违规计数 + 月度复盘约定 |
| 低：benchmark→档案 PR 式审批 | `capability_ingest.py --diff/--apply`：默认只生成 `pending-ingest-diff.json`（baseHash + 逐维 old/new/caseIds），`--apply` 校验 baseHash/revision 一致才合入（档案被改则拒绝，需重新 diff）；`--direct` 保留低风险直写；控制台显示待审批数量 |
| 低：UI 展示档案版本与新鲜度 | 总览/自改进 Tab 显示 revision、最后更新时间、7 天 stale 徽章与 reasons；`council_status` 工具同步增强 |

### v15.2（2026-08-24 元评审 `runs/2026-08-24_19-13-33` → 落地映射，见 evidence/2026-08-24.md）

元评审结论「有条件通过」：设计骨架成立，但护栏触发率/成本漂移/SLA/反馈环同源/benchmark 双引擎是四类系统性问题。全部 13 项已落地：

| 编号 | 问题 | 落地实现 |
|---|---|---|
| P0-1 | 根因多为推断 | `evidence/2026-08-24.md`（护栏/成本/SLA/失败 RCA/同源/基准债务逐条证据化） |
| P0-2 | 护栏 528 次/24h | selector 静态不合格候选（identityUnknown/stable=false/allowlist/自验/档位超限）评分前预过滤，每次选择只记一条 `pool_excluded` 汇总事件；identity_unknown 阈值标签改 required=true；事件带 capabilitiesRevision |
| P0-3 | 成本 drift −26% | est/actual 同口径三分离（estBase 对账 / planCny 预算 / actual 真实）；`token_profiles.py` EWMA 取代硬编码 est_out；cost_calibrate 按 model×role 拆分 + `--check` 告警（>10% 落 cost-reconcile-events.jsonl，插件每日 04:00 对账）；query_balance monthly_estimate 死代码修复；M3 promptTokens 补读 |
| P0-4 | SLA 超标 | 档位硬约束 maxThinkingRank/maxSubtasks/wallBudgetS（fast 240s）；terminator 墙钟预算最先判定；runtime/cost 遥测每 run 自动回填（复活 latency 项）；budget 预检按轮放大（×maxIter×1.7）；插件 timeoutMs 按档位 |
| P0-5 | 反馈环同源 | verifier 强制跨厂商（exec provider 全量互斥）；feedback 行 scoredBy；requireHeteroScorer 过滤同源行；能力分回写默认 **pending diff**（`update_capabilities.py --apply` 人工审批 + baseHash 校验），autoApply 才直写；judge 漂移 ≥0.5 暂停写档案 |
| P1-1 | failed_runs 2 条 | RCA 标注（1 条已由 v15.1 修复、1 条加 decompose fallback）；terminator/墙钟回归测试固化 |
| P1-2 | judge 漂移单点 | 基线锁定 promptHash+answerHash（变更强制重建并落事件）；双 judge（M3+deepseek-flash）交叉验证；holdout 子集独立漂移 |
| P1-3 | 绝对评分强者愈强 | `pairwise.py` Elo 横向比较（同 run z 校准后两两胜负，K=32），selector 低 Elo 软惩罚不剔除 |
| P1-4 | 可视化缺口 | 控制台护栏分布条/SLA 热力条/rework 原因占比/成本 drift 曲线/对账告警/fx 等级/pending runtime diff 卡片；/metrics fx_stale_level + reconcile_alerted |
| P2-1 | 非 Pareto 最优 | selector `_apply_pareto` 前沿软加分（params.paretoEnabled 默认关） |
| P2-2 | 汇率 stale 无停机 | `selector.fx_status()` 黄灯(≥1 交易日)/停机(≥3 交易日)；run_council 停机时 exit 1 拒绝开跑；插件 `fx_status.py` 前置检查 |
| P2-3 | golden 无防污染 | golden 12 条（provenance/holdout/异源 58%）+ `golden_guard.py` 契约（provenance/异源≥30%/与用例无交集/contentHash）+ cross_judge 前缀互斥加固 |
| P0-6 | benchmark 双引擎 | `benchmark/archive/2026-08-24/`（726 文件）；v21-cases.json semver+contentHash；ingest casesHash 联动（用例漂移拒绝合入）；scores 空目录显式告警；finish_benchmark 改走 ingest 审批流；.token/.opencode_key 删除 |

测试：orchestrator 51 + benchmark 12 = 63 项全绿（新增 `test_v152.py` 18 项、`test_golden_guard.py` 6 项）。

### v15.3（2026-08-24 第二次元评审 `runs/2026-08-24_20-18-15` → 落地映射）

元评审结论：框架完整，但「评估的尺子是脏的」——能力分同质化、护栏刷屏、成本估算系统性漂移、fast 档 SLA/成本双超、金标样本不足。**7 项全部落地**（含主会话一手实证修正）：

| 编号 | 问题（评审报告 + 主会话实证） | 落地实现 |
|---|---|---|
| R1 | 护栏 575 次/24h（identity_unknown 306 + self_verify_ban 255） | **实证**：306 条 100% 来自 stealth/ox-alpha 三档（各 102 次，v15.2 预过滤部署前的存量，19:14 后独立事件归零）；255 条 100% 来自 MiniMax-M3 五档自验禁令。**治理**：①ox-alpha 走**身份锚定**（见 R2）而非退役——maintainer 已在 settings.yaml 正式配置 `openrouter-stealth`（路由+key），档案标记（build_capabilities.py 按 "stealth" 前缀写死 identityUnknown/stable）与真实配置脱节是根因；②`selector._POOL_EVENTS_SEEN` 同 run 排除集合去重（~10 条/run → ~2 条/run）；③`retire_candidate.py`/`anchor_candidate.py` 候选生命周期脚本（安全契约：只动 identityUnknown/unstable 条目、备份、revision 自增、caps_guard 校验、pending diff baseHash 同步） |
| R2 | ox-alpha 无法调用（council 报告建议「锚定或移除」） | **正式接入**：`stream_llm.py` 加 OpenRouter 流式路由（OpenAI-compatible SSE，reasoning.effort 三档）；`config_loader` 加 OPENROUTER_URL/key；`pricing-profiles.json` 加 openrouter 免费池条目（实测 models API pricing=0）；档案锚定 identityUnknown=false/stable=true（revision 5→6）；`build_capabilities.py` 改 `CANDIDATE_STATUS` 显式状态表。**接入排障实录（verdict-raw 落盘 + HTTP 状态码诊断揪出 4 连环坑）**：①档案 cid 把 '/' 编码为 '--'（`stealth--ox-alpha`），请求 OpenRouter 报 400 is not a valid model ID——**openrouter_stream 请求前还原模型 ID**；②`_stream_lines` HTTPError 分支曾在 meta 未绑定时赋值 → UnboundLocalError 吞掉真错误（已改局部变量）；③usage chunk 的 choices 可为空数组，usage 检查必须先于 choices 判断（否则对账数据丢失）；④`_write_circuit`/token_profiles 写盘无锁，多线程/多进程并发 tmp+replace 撞 WinError 32（线程锁+退避重试）。附带：`_stream_lines` 记 HTTP 状态码+响应体；OpenRouter 瞬态 429/5xx 一次退避重试；verifier 原始输出落盘 `verdict-raw-r*-s*.json` |
| R3 | 成本 drift −15.87% 单向漂移无收敛机制 | 校准闭环：`token_profiles.calib_for/update_calib` per-(model,role) 系数（EWMA 0.3、钳制 [0.5,2.0]、样本 n≥5 启用）；`cost_calibrate.py --check/--recalibrate` 每日对账自动回填（实测 verifier calib 0.894 / synthesize 1.099 立即生效）；`effective_cost_cny(calib_role=…)` 与 `_cost_pair(role=…)` 全链路应用 |
| R4 | fast 档 SLA/成本双超（P95 224.6s>120s、单 run ¥0.0534>¥0.03） | 轮前硬护栏：每轮 assigning 后预检「预估累计成本 > budget×costGuardMul(1.2) 或预估墙钟突破 wallBudgetS」→ forced（此前只在轮后检查，最后一轮可推高总额）；fast 档 `maxTaskTokens=4000`（超大任务 decompose 前提示降档）；params 外置 costGuardMul/maxTaskTokens。样本仅 5 个，θ/maxIter 等架构参数不动，观察 7 日数据再校准 |
| R5 | 能力分同质化（9.5-10，selector 退化） | `selector.build_rank_table` rank 归一化（per-dim 排序位置映射 0-10、并列取平均排名）；`score_candidate` 归一化空间内完成 fallback/shrinkage；`selection.rankNormalize`（默认开）可回退；实测区分度从 1.7 展开到 7.4 分位；测试 `test_rank_normalize_restores_discrimination`/`test_rank_table_ties_average` |
| R6 | 金标 12 条撑不起 9 维/16 条目自进化 | `golden-set.json` 12→36 条：9 维每维 ≥3、异源 63.9%、holdout 10 条、对抗用例 9 条、难度 30/50/20、全中文；`golden_guard.py` 契约全过（contentHash 重算）；judge_drift 基线强制重建（promptHash 锁定，变更自动重建）；init 模式不再白跑第二 judge（省 36 次调用） |
| R7 | failed_runs 失败原因未结构化 | 插件 `logFailedRun` 加 errorCode（timeout/exit_N/no_interpreter）/stage/promptHash（sha256 前 12）；同 errorCode 7 日 ≥3 自动行内标 issue；council_status 展示「重复失败模式」告警（failedPatterns）；历史 2 条均已 RCA 闭环 |

**v15.3 新增/变更文件**：`orchestrator/retire_candidate.py`、`orchestrator/anchor_candidate.py`（候选生命周期）；`stream_llm.py`（openrouter 路由+HTTP 诊断+重试）；`config_loader.py`（OPENROUTER）；`selector.py`（rank 归一化+pool_excluded 去重+calib）；`token_profiles.py`（calib）；`cost_calibrate.py`（recalibrate）；`council_v14.py`（_provider_of/轮前护栏/_cost_pair role/maxTaskTokens）；`params.py`+`council-params.json`（costGuardMul/maxTaskTokens/rankNormalize）；`build_capabilities.py`（CANDIDATE_STATUS）；`pricing-profiles.json`（openrouter 免费池）；插件 `index.js`（failed_runs 结构化+重复模式）；`benchmark/golden/golden-set.json`（12→36）。

测试：orchestrator + benchmark 全量 **66 项全绿**（新增 rank 归一化 2 项；`test_v152.py` 去重缓存测试隔离）。

### v15.4（成本哲学重构 + benchmark 自进化，2026-08-24 maintainer 逐条拍板，**已实施**）

**实施状态（2026-08-25）**：全部 41 条决议落地。核心代码 pytest 66 项全绿；fast 档真实 run 验证通过（执行者轮换 3 家各执行 1 子任务、动态验证者 k=3 异厂商互斥、S_r 正常）；补题流水线已产出并晋升 4 题入考卷。

**核心原则（maintainer 原话语义）**：council 的终极使命是出结果，不是省钱。算钱的目的是为了**选择适当的模型**（能力强、性价比高、还有足够的钱去跑），而不是控制模型的用量。同样的成本不换更差结果，同样的结果不多付钱；更高成本换更好结果，完全接受。

#### A. 汇率（删除停机拒绝，三级 fallback）

1. **删除**"stale ≥3 交易日拒绝运行"的停机检查（`fx_status` 停机等级不再拦 run_council；黄灯/停机降级为成本置信度标记：stale 时成本项降权，选择更依赖能力分）；
2. 三级 fallback：**主源 CFETS → 备用 API（须实测验证可用）→ 再失败用最近一次成功更新的汇率**（`fetch_exchange_rate.py` 加备用公开源 + fallback 链；`exchange-rates.json` 增加 fallbackUsed/sourceChain 字段）；
3. fallback 链全失败 → run 结果带 `fx_warning` 告知主会话"汇率 API 皆不可用，建议修复"，并写 `fx-events.jsonl`；
4. 理由（maintainer）：单次 run 消耗仅几分钱，汇率暴涨暴跌影响微乎其微。

#### B. 成本与模型选择（λ 保留但温和化）

5. λ 保留：语义改为**能力分主导、成本温和微调、同分 tie-break**（惩罚封顶大幅调低，deep 档 λ≈0）；λ 数值由主会话定，符合 maintainer 设计意图；
6. `selection.paretoEnabled` 打开（Pareto 前沿软加分，"没人比你又强又便宜就加分"）；
7. **延迟数据冷启动代理**：`runtime.latencyP50Ms` 缺失时按 thinking 档位给保守代理值（档位越高假设越慢），遥测回填后自动切真实值——防止 max 档被误判为与 off 一样快。

#### C. 预算预检（余额感知，不拒派）

8. `budget.precheck` 改造为**余额感知报告**：预计消耗 vs 各 provider 余额/额度/覆盖率，只报告不拒派（删除 `budget_precheck_over` 拒绝路径）；
9. 预检接入 token EWMA + per-(model,role) 校准系数（v15.3 校准闭环的消费端）。

#### D. 预算与护栏（钱维度护栏全删，墙钟统一）

10. **删除** v15.3 轮前成本护栏（costGuardMul 预检）与 terminator 成本 forced；
11. **删除档位预算**（tiers.*.budgetCny 字段移除）；`sla.*.costCapCny` 降为纯观察指标（继续统计、不判违规、不告警）；
12. 防失控 = maxIter（3/5/6）+ 全局 maxRounds 8 + 余额耗尽护栏（quota_factor → inf 踢出候选）+ 墙钟（**v15.5 修订：maxIter/maxRounds 删除，防失控 = 增长枯竭判据 + 墙钟 + stalled + 余额耗尽，见 §14-v15.5-A**）；
13. **墙钟统一 1800s（30 分钟）三档同值**，语义重定位为"防模型异常的最后防线"（不是质量预算）；宿主 runPy 超时统一 **1920s**（墙钟+120s 收尾余量）；单次模型调用 total 900s、idle 180s 不变；轮前墙钟预检保留（技术防呆）（**v15.5 修订：墙钟改动态预算（基础 + Σ 规模估时、留 ≥240s 宿主余量）；异步化方案 B 单独立项，见 §14-v15.5-B**）；
14. 档位本质 = 复杂度刻度：**θ（7.0/8.0/8.5）+ maxIter（3/5/6）+ maxSubtasks（3/4/4）**——复杂度是因、时间是果（**v15.5 修订：θ 三档统一 9.5、maxIter 删除；档位 = 并行规模（maxSubtasks + 双路执行 + 验证者数），见 §14-v15.5-A**）；
15. **删除 maxThinkingRank**：每个档位视作不同模型，完全由 selector 自主决定（配合 B-7 延迟代理）；`maxTaskTokens`（fast 4000）保留（速度技术护栏性质）。

#### E. 模型参与动态化（防全职化 + 动态角色）

16. 执行者：每子任务 1 家，留 `maxExecutorsPerSubtask=1` 参数位；
17. **验证者数量动态化**：`k = clamp(可用异厂商 baseModel 数 − 1, 1, 3)`；验证者之间 baseModel 互斥，与执行者互斥（hetero 原则保留）；参数位 `verification.minVerifiers/maxVerifiers`（**v15.5 修订：异厂商以 vendorGroup 厂商计、verifier 之间必须厂商互斥——修复 v4-pro/v4-flash 同厂商互评漏洞，见 §14-v15.5-C**）；
18. 多 verdict 合成：`hardGateFailed` 取并集、`overallScore` 取均值、`reworkList` 并集去重；feedback 行 `scoredBy` 改列表；
19. **层次 1 执行者轮换**（防全职化，微软 MAF GroupChat round-robin 同款）：每 baseModel 轮内最多执行 ⌈子任务数/可用 baseModel 数⌉ 个子任务，跨轮轮换组合；
20. **层次 2 双路执行互评**（standard/deep 开，fast 只做层次 1）：同一子任务 A、B 都执行，A 评 B、B 评 A，取分高者为正式产出，另一份保留供 synthesize 参考——"同轮不评自己 + 跨轮人人有份"；
21. 并发线程池跟"子任务数 × (1+k)"走，上限 8；
22. verdict 绑定**输出指纹**（exec 输出 hash，合成阶段校验，防串轮/串子任务错评）；verdict-raw 落盘已有；
23. reworkList 每项带 `priority`（高/中/低），下一轮按优先级返工；
24. 广播语义：verifier 拿到全局上下文（含其他子任务产出与验证结果），可发现子任务间矛盾。

#### F. Benchmark 自进化（全程自动化无人值守）

25. **考题生命周期打通**：AI 出题 → 异厂商双盲复核+仲裁 → 进金标池（校准 judge + 积累健康度）→ 观察期后晋升考卷池（v21-cases）→ 区分度衰减 → 退役归档。金标池=育苗田，考卷池=生产田，同一流水线（**v15.5 修订：观察期 ≥3 天此前仅文档声称、代码未实现——v15.5 F-18 落实；金标升级评卷考场后另担「考评委」角色，见 §14-v15.5-E**）；
26. `benchmark/golden_evolve.py`：`--expand`（补题）/ `--health-check`（健康度）/ `--replace`（替换低健康度题）+ `--auto`（组合模式，挂每日 04:30 自评后）；
27. golden_guard 契约变更：`author` 允许 `"ai"`，必须带 `reviewedBy`（≥2 家异厂商复核模型）+ 仲裁记录；
28. **换题重跑 = 内容 diff 增量**（方案 A，逐题比对内容哈希，只重跑变化题+新题；>50% 题变化自动升级为全量）；成绩记录绑定 `caseHash`（题目内容版本），换内容即新成绩、旧成绩作废；
29. 增量与全量成绩可比性：客观题零差异（确定性判分）、LLM judge 题用**金标锚定**校准尺度、成绩带 caseHash；
30. 全自动合入安全网（替代人工审批）：写前校验（caps_guard + baseHash/casesHash）→ 体检前置（judge 漂移 <0.5 才放行回写）→ 回归门禁（regression_gate）→ **熔断**（门禁连续 3 次拦截 / 漂移持续超阈值 7 天 → 自动暂停自进化 + council_status 告警）+ 备份回滚（capabilities.rev{N}.bak）+ 全量审计日志；
31. `feedback.autoApply=true`：04:30 漂移体检通过后自动 apply 累计 pending diff；
32. 每日调度表：**02:00** 换题检测（contentHash 比对）→ 有变化自动重跑+门禁+合入；**04:00** 成本对账+校准回填；**04:30** judge 自评 → 体检通过则自动 apply → 金标健康度+自动换题；**09:30** 汇率（新三级 fallback）。

#### G. 题量与精度治理（统计闭环，maintainer 拍板目标）

33. √n 定律：题数翻倍精度只增 41%，边际递减是数学事实；最佳值动态确定：`n = (z × σ / 目标精度)²`，σ 每次跑分后自动重算；
34. **目标参数（maintainer 拍板）**：考卷每维 95% CI ≤ **±1 分**；金标最小可检漂移 **0.5 分**（实测 35 条有效、σ=1.15 → 可检 ±0.38 分，**36 条已达标，取消"扩到 54"计划**——54 是拍脑袋数字，被实测推翻；对抗用例 ≥10 通过换题时顺带补齐）；
35. **实测基线（2026-08-24）**：每维仅 2-4 题严重不足——code σ=1.48（±1 分目标需 9 题）、chinese 10、research 9、reasoning 7、instruction/long_context 5、creativity 3、tool_use 2、safety 1；能力分同质化（9.5-10）的另一半真相是测量噪声（code 维度 95%CI ±1.45 分，模型差 1 分都测不出）；
36. **每维达标线 = ±1 分 AND 分布健康**，不设平均题数线（维度间题数可不同，达标即可，"不能为了题少而少"）；
37. **分布健康四指标**（体检在绝对分 0-10 上做）：范围（挤窄带=题太简单）/ 天花板地板率（>30% 模型满分=题组失效）/ 断层（相邻名次跳变=缺中等难度题）/ 题区分度系数（该题得分与模型总体水平相关性低=噪声题换掉）；判别信号：σ_within 大=题量不足补题，分布畸形但 σ 正常=题质量不佳换题；
38. **百分制 rank 映射**（maintainer：第一名 100 分、最末名 1 分）：档案存绝对分（自进化 EMA 在绝对空间融合），选择/展示层动态 rank 映射到 1-100（排名线性映射）；顺序约束：先补题达标+分布健康，再在健康分上做 rank（防畸形分布被 rank 掩盖、噪声放大成假差距）；
39. 题数与模型数量：金标条数**无关**（考 judge 尺度）；考卷题数间接相关（m 个模型相邻名次分差 ≈10/m，模型池大幅扩张时需把目标从 ±1 分收紧，触发重算）。**（v15.5 修订：金标升级评卷考场后，「金标条数与模型能力分无关」仍成立，但金标条数直接决定 judge 档案测量精度——按 √n 提升，见 §14-v15.5-E）**

#### H. 借鉴来源（已记录）

40. 微软 MAF GroupChat：round-robin 轮换（官方示例，E-19）、动态角色无全职岗位（E-20 角色互换）、Agent 主持人模式（selector+terminator+synthesizer 合并为模型决策，**v15.5 演进方向，不实现**）、max_rounds+termination_condition 防失控（印证 D-12/13）；
41. Leige GameDev 工作流：角色分离与轮间互换的调和（"同轮不评自己+跨轮人人有份"）、verdict 输出指纹（E-22）、rework 优先级（E-23）、"总控亲手验证"→ verifier 独立事实核实（**v15.5 方向**：verifier 带检索工具，host-bridge 主会话 web_search 代做已有规划）。

#### 实施清单（v15.4 全部 41 条已落地，2026-08-25 起本清单转为历史记录）

- selector.py：λ 温和化 + paretoEnabled + 延迟档位代理 + 删 maxThinkingRank + 轮换分配
- council_v14.py：删轮前成本护栏 + verifier 动态化/双路互评/轮换 + verdict 指纹 + rework 优先级 + 广播上下文
- terminator.py：删成本 forced；params.py + council-params.json：删 budgetCny/costGuardMul/maxThinkingRank，墙钟统一 1800，verification.* 参数位
- budget.py：余额感知报告；sla_stats.py：成本降纯观察
- fetch_exchange_rate.py：备用源+fallback 链；插件 index.js：删 fx 停机前置、宿主超时统一 1920s、每日 02:00 换题检测任务、04:30 自动 apply + golden_evolve 挂接、autoApply
- benchmark：golden_evolve.py（expand/health-check/replace/auto）、考题生命周期晋升/退役、逐题 contentHash diff 重跑、成绩 caseHash、金标锚定 judge 题、分布健康四指标、百分制 rank 映射层
- 测试：全部相关用例更新 + 新指标测试；DESIGN §15 数据契约同步（score 仍是 [0,10] 绝对分，rank 层为派生数据）

#### 实施记录（2026-08-25 补充：流水线实证发现）

- **复核拦截实例**：ox-alpha 出的 code 推演题答案算错（45/98 vs 正确 44/35），M3+flash 两家异厂商复核独立推演均判 fail——「AI 出题 + 异厂商复核」安全网按设计生效；code 维度出题改由 M3 承担（`GEN_MODEL_BY_DIM`）；
- **JSON 宽松解析**：ox 在 JSON 字符串内输出裸换行（非法 JSON）→ `_extract_json` 加状态机修复（只转义字符串内换行）+ fence 优先提取；
- **免费池瞬时故障**：M3 出题遇 SSL UNEXPECTED_EOF（0.2s 挂断）→ `_call` 加轻量重试；
- **晋升=移栽**：金标晋升考卷后**从金标池移除**（防「与考卷无交集」契约违约）；`promote_healthy` 带历史晋升残留清理；
- **仲裁契约**：reviewedBy 存在非 pass 复核时，须有 arbitration.verdict=pass 才合法（golden_guard v15.4 修订）。

### v15.5（递归评审架构 + 判停标准重构，2026-08-25 maintainer 逐条拍板，**设计决议，尚未开工**）

> 触发：第三次元评审 `runs/2026-08-25_03-48-54`（16 条问题清单见 `meta-review-2026-08-25.md`，含主会话实证修正）+ 六轮讨论逐条拍板。
> 实施路线：`v15.5-roadmap.md`（Phase 0-5 分批，每批验证通过再进下一批）。本节为设计决议正文。

#### A. 判停标准重构（maintainer 拍板：只追求最佳收敛，不设轮数上限）

1. θ 三档统一 **9.5**（原 7.0/8.0/8.5）。语义重定位：θ=9.5 是「理论满分天花板」而非常规达标线——S_r ≈ 本轮 verifier 平均打分（calibration.aggregate 加回公共均值），verifier 极少给出 9.5+，**多数 run 将以 early_stop（增长枯竭）收尾，converged 变稀有（预期内）**；报告状态文案同步调整。
2. 「增长枯竭」判据重定义：**近 3 轮滑动窗口内 S_r 极差 < δ=0.2**（原：相邻两轮 ΔS < 0.3）——防 ±0.3 震荡永不停；δ 收紧至 0.2。
3. **删除轮数上限**：档位 maxIter（3/5/6）与全局 maxRounds（8，经查为死参数）不再触发 forced。防失控 = 增长枯竭 + 墙钟 + 硬门禁 stalled（同问题 2 轮无改善，保留）+ 余额耗尽护栏。
4. 跨轮可比性：S_r 的 z 归一化每轮独立 + verifier 每轮轮换 → 跨轮比较有噪声；3 轮窗口极差是首选平滑手段，试跑后仍抖动则升级为「只比较 verifier 集合一致的轮次」或 5 轮窗口/回归斜率判据。

#### B. 墙钟（时间随规模缩放；方案 A 先行 + 方案 B 单独立项）

5. **方案 A（先行）**：run 墙钟从「统一 1800s」改**动态预算** = 基础值 + Σ(子任务×执行者×验证者) 规模估时；上限 = 宿主工具超时（1920s）− **≥240s 收尾余量**（synthesize+落盘防宿主误杀）。
6. **方案 B（异步化，单独立项）**：run_council 改「派发模式」——工具立即返回 runId，后台执行，完成推送报告。进度汇报两层：**里程碑事件**（启动/每轮结束 S_r·耗时·成本/收敛判定/完成）+ **90s 心跳兜底**（防误判卡死）；fast 档仅完成时一报。总时长彻底弹性、不设上限，不受宿主 1920s 工具超时约束。
7. 单次模型调用超时不变（idle 180s 主判 + total 900s 保险丝，§6.5）。

#### C. verifier 厂商互斥（3A）

8. 新增 **vendorGroup 厂商分组**：同一厂商的多个 baseModel（如 deepseek-v4-pro 与 deepseek-v4-flash）归同一 vendorGroup。**「异厂商」以 vendorGroup 计，不再数 baseModel 条目**——修复 v15.4 E-17 漏洞（实测：2026-08-25 元评审 run s1 出现 v4-pro+v4-flash 两个 DeepSeek 系 verifier 互评，交叉验证独立性打折）。
9. k = clamp(可用 vendorGroup 数 − 1, 1, 3)；verifier 之间必须厂商互斥，与执行者 provider 全量互斥（hetero 原则不变）。

#### D. 角色动态化（3C）+ JSON 解析四层防线

10. synthesize/decompose 不再写死模型：按角色能力向量经 selector 动态选（synthesize：long_context/instruction_following/chinese；decompose：reasoning/instruction_following）；输出结构校验（synthesize 必含结论/置信度等字段；decompose 必须产出合法子任务列表，已有兜底）+ 角色绑定日志（decisions.jsonl 记「角色=谁、为什么」）。
11. **JSON 解析四层防线**（修复实测 M3 verdict `parsed:false` 被静默丢弃）：①公共 lenient 解析器（fence 剥离 + 裸换行状态机 + 截断修复 + 未转义引号启发式，verdict/decompose 统一使用——原 `_extract_json` 为贪婪 `{.*}` 原始版，golden_evolve 的 lenient 版未推广）；②关键字段降级抠取（overallScore/hardGateFailed/reworkList）；③诊断重试一次；④异厂商替补补位（保证 k 不缩水）。verdict-raw 日志改存全文，失败可复诊。验证标准：pytest 全绿 + 真实 run `parsed:false` 归零。

#### E. judge 递归架构（3B，单向数据流）

12. **金标升级「评卷考场」**：每条金标补 1-2 个劣质答案变体 + 期望分标签（现有金标只有好答案，测不出区分度）。judge 档案 = 标定准确度（打分 vs 期望分）/ 区分度（好答案分 − 坏答案分）/ 稳定性（漂移监控已有数据）/ rubric 服从。
13. **judge 选择依据 = 评卷考试分，不是做题分**：benchmark 考「做题」，金标考「评卷」，各管各的角色池；做题分只管执行者/分解器/汇总者。
14. **任期制 + 锚点 + 影子 challenger + 换任交接**：judge 每周重选（不天天换，漂移监控需连续基线）；holdout 子集作锚点（跨 judge 可比）；challenger 每天同打锚点集，显著更优触发换任评估；换任交接 = 新旧 judge 锚点双打分算 offset → 新基线按 offset 折算接续（历史趋势不清零）→ 换任后 3 天保守期只告警不执行 pauseWrite。
15. **四锚（递归的根）**：A1 机器判分锚（代码/数学题标准答案由解释器验证，金标中占比保底）｜A2 不可变 holdout（递归流程只读）｜A3 **人工锚主动推送**（每周推 5 题到主会话，二元判断「通过/有疑问」，不打分；有疑问自动转第三家异厂商仲裁；不让 maintainer 自记）｜A4 时间尺度分层（能力档案最快 → 考卷日级 → 金标周级且跨任期观察；内环快外环慢）。
16. 递归良性循环：金标自我更新 → judge 档案测量精度按 √n 提升 → 「谁适合当 judge」越判越准；金标供题 + 质量闸 → 考卷自我更新 → 能力档案逼近真实能力。**成立条件 = 四锚齐全**（无锚的模型闭环会回声室漂移）。

#### F. 出题计划区分度目标

17. 考卷每维题数目标 = CI 达标（±1 分，已有）+ **区分度缺口加权**：维度内模型间分数拉不开（方差过小）→ 加题；题区分度 |r|<0.2 → 退役（已有）；出题质量由金标体系确定（双盲复核 + 机器可判 expected，已有）。
18. 实现 v15.4 F-25 声称的「观察期 ≥3 天」晋升条件（此前仅文档声称，代码未实现）。

#### G. selector 分层（几十个模型时的理想逻辑）

19. **L1 厂商出线**：按 vendorGroup 分组，每家只让「当前最强」的 1-2 个条目进入本轮候选（同厂商 10 个型号错误高度相关，全放进去只会刷爆多样性奖励）；**L2 角色向量**匹配；**L3 排序** = 能力 rank × 角色胜任分 − λ成本 − μ延迟 + 多样性 + **UCB 不确定度加权**（score = 均值 + c×√(ln t/n)，处理初分噪声）；**L4 配额轮换**（厂商配额 + 防全职化）。
20. **新模型入池协议（v15.5-K 修订：手动加入池 → 手动跑分 → 得初分后参与选择）**：初分 + 置信区间与在位者比较 → 厂商内 challenger 挑战制（综合分决定替换/轮换出场）→ 实战反馈校准初分。**新旧考卷版本可比性**：成绩绑 caseHash（已有），新模型初分只与「当前考卷版」在位者分比较，与旧版分比较用 z 对齐。
21. 边际效应共识：council 质量上限 = 独立厂商数 × 每厂商最强模型；多模型用在「并行干活」（子任务拆分/双路执行），不用于「同一答案堆票」。

#### H. 元评审问题 → 实施映射

第三次元评审 16 条（`meta-review-2026-08-25.md`）与讨论共识 D1-D9 全部映射至 `v15.5-roadmap.md` 的 Phase 0-5：**Phase 0** 参数/口径小修（maxRounds 死参数退役、轮数显示口径、面板新增/存量拆分+趋势、SLA 目标值对齐）；**Phase 1** 判停+动态墙钟（试跑观察）；**Phase 2** 解析防线+3C；**Phase 3** 3A+子任务覆盖修复（分解器子任务数与执行容量匹配，防 no_candidate 结构性轮空）+被拒样本隔离；**Phase 4** 递归架构（先出设计文档）；**Phase 5** 异步化+可靠性收尾（failed_runs 处置链路、成本 drift 超线 24h 自动校准、档案健康字段）。

#### I. v15.5 未纳入本轮（沿用 v15.4 H-40/41 预告）

- Agent 主持人模式（selector+terminator+synthesizer 合并为模型决策）：继续作为演进方向，本轮不实现。
- verifier 自带检索工具：事实断言检索仍由 host-bridge 主会话 web_search 代做。

#### J. 测量协议对齐（2026-08-25 maintainer 拍板：单一数据源，方案细节见 `tier-alignment-plan.md`）

22. **插件桥**：host-bridge plugin 插件用 `ctx.llm.listModels` / `resolveModelInfo` 生成 `model-tier-bridge.json`（每模型：档位枚举 + wire 拼写 + contextWindow + defaultMaxTokens/capabilityMaxTokens），Python 侧唯一数据源；模型目录变更自动跟随，永无手填漂移。
23. **档位协议与 host-bridge 主会话同源**：DeepSeek 档位 off/low/high/max，wire = `thinking:{type:'disabled'}` / 官方顶层 `reasoning_effort`（废止 budget_tokens 512/4096/16384 双套映射）；pi-ai 系（M3/ox）按 thinkingLevelMap 拼写；M3 档位枚举与拼写以 host-bridge 目录为准（待确定点 T1：读 pi-ai 导出或实测）。**测的档位 = 用的档位，能力档案测量有效性恢复**。
24. **max_tokens 还原模型上限**：benchmark 取 capabilityMaxTokens、orchestrator 取 defaultMaxTokens（host-bridge 侧默认 256000/131072 等），删除 32768/8192/16384 自定义值；max 档「想满挤没答案」坑随之消失。
25. **插值废除 + 全档位×全案例**：`build_capabilities.py` 删插值；CANDIDATES = 桥文件全档位；§3 分档策略段同步改写。
26. **连锁执行**：现有 12 档旧成绩归档（`benchmark/archive/2026-08-25-pre-alignment/`）→ 新协议全量重跑（4-8h 断点续传，成本重估 ¥15-25 + M3 跨窗口分批）→ 档案重建。响应文件追加 `thinking` 参数落盘（数据溯源）。
27. **顺序约束**：Phase 1（判停标准）必须在协议对齐 + 档案重建之后——不在未对齐的档案上做判停实验。

#### K. 模型池管理（2026-08-25 maintainer 拍板：增=手动、删=自动）

28. **池名单显式化**：`model-pool.json` 成为候选池的成员准入第一层（model 粒度，状态 active / retired-by-user / retired-by-host-bridge-removal + 时间戳审计）。`build_capabilities` 只为池成员生成档案条目，`selector` 只从池成员选——取代「跑过 benchmark 就算成员」的隐式逻辑，`stable` 标记职责并入池名单。
29. **增加模型（手动）**：控制台「＋增加模型」弹下拉菜单，数据源 = host-bridge 模型目录（与主会话模型选择同一 `ctx.llm.listModels` 数据源，内容天然一致）；选中即入池，**全档位按桥文件目录展开**，分数显示「未测量」。
30. **跑分（手动）**：每成员一个「跑分」按钮，手动触发全档位×全案例 benchmark（断点续传 + 完成通知）；**入池不自动跑分**（maintainer 拍板，控制跑分时机）。「未测量」成员不参与选择，跑分 + ingest 后才入选择池。
31. **删除模型（自动为主）**：①手动删除 = 从池移除，档案成绩保留只读；②**host-bridge 删模型 → 自动同步删除**：插件启动 + 模型目录变更时比对，池成员不在 host-bridge 目录即自动退役（成绩保留只读、审计事件），控制台直接消失不标红（maintainer 拍板）。
32. **与 G-20 衔接**：新模型入池协议更新为「手动加入（全档位）→ 手动跑分得初分 → 参与选择（初分+置信区间+厂商内 challenger）」，取代「必先跑 benchmark 得初分再入池」的旧表述。
33. **UI 三决策（2026-08-25 shape 定稿，见 `ui-model-pool-brief.md`）**：①列表 = 模型分组行 + 可展开档位子行；②stable 切换按钮退役（成员身份由池名单唯一决定）；③退役模型 Tab② 完全隐藏、审计事件表可见。Operate 模式，沿用 ccl 样式 + DSW tokens + SVG 雷达图，不动其他五 Tab。

### v15.5 对 v15.4/v15.3/v15.2 的修订

- v15.4 D-12 的「防失控 = maxIter + maxRounds」：**v15.5 删除轮数上限**（A-3），防失控 = 增长枯竭 + 墙钟 + stalled + 余额耗尽。
- v15.4 D-13 的「墙钟统一 1800s」：**v15.5 改动态预算**（B-5）；异步化方案 B 单独立项（B-6）。
- v15.4 D-14 的「档位 = θ(7.0/8.0/8.5) + maxIter + maxSubtasks」：**v15.5 θ 三档统一 9.5、删 maxIter**（A-1/3）；档位 = 并行规模（maxSubtasks + 双路执行 + 验证者数）。
- v15.4 E-17 的「k = clamp(异厂商 baseModel 数 − 1, 1, 3)」：**v15.5 改 vendorGroup 厂商计 + verifier 厂商互斥**（C-8/9）。
- v15.4 E-16 的 `maxExecutorsPerSubtask=1` 参数位与 E-20 双路执行（实际 n_exec=2）不一致：**v15.5 Phase 1 实施时统一**（参数位删除或接入 n_exec）。
- v15.4 G-39 的「金标条数无关」：**v15.5 修订**——金标升级评卷考场后，金标条数直接决定 judge 档案测量精度；「与模型能力分无关」仍成立（E-12）。
- v15.4 F-25 的「观察期后晋升」：观察期 ≥3 天在 v15.4 仅文档声称；**v15.5 落实实现**（F-18）。
- §12.5 参数/结构级分层矛盾：**θ 归结构级**（收敛判据，改动需 maintainer 终审，本次 9.5 即拍板）——修正 θ 同时出现在参数级（全自动）与结构级（收敛判据）的矛盾；δ/窗口等判据数值的带内微调仍参数级。
- §4.1 θ 动态校准「目标=80% 任务 2 轮内收敛」：**v15.5 失效**——θ=9.5 下多数 run 以 early_stop 收尾，该校准目标作废。
- §6 终止策略的 max_iter/max_rounds/δ=0.3：**v15.5 全部失效**，以 §14-v15.5-A 为准。
- §3 Benchmark 分档策略（2-3 代表档位 + 线性插值）：**v15.5 废止**，改全档位×全案例（J-25）。
- §3/§9 档位枚举与 budget_tokens/max_tokens 手填映射：**v15.5 废止**，改 host-bridge 插件桥单一数据源（J-22/23/24）。



### v15.4 对 v15.3/v15.2 的修订

- v15.3 R4 的 costGuardMul 轮前成本护栏：**v15.4 删除**（D-10）；
- v15.3 R4 的 maxTaskTokens：保留（D-15）；
- v15.3 轮前墙钟预检：保留（D-13）；
- v15.3 R6 的"金标扩到 54"：**取消**，实测 36 条已满足 0.5 分漂移检测（G-34）；
- v15.2 P0-4 的 maxThinkingRank 档位硬约束：**v15.4 删除**（D-15）；
- v15.2 P2-2 的汇率停机拒绝开跑：**v15.4 删除**（A-1）；
- v15.2 P0-5 的 pending diff 人工审批：**v15.4 改全自动合入**（F-30/31）。

## 15. 数据契约与失败处理（v15.1 固化，评审报告 §5/§6 风险对策）

**数值 schema（写死进代码 + 测试）**：
- 百分比一律整数语义（96=96%），入口统一过 `norm_percent`；
- 价格字段带单位后缀（`*CnyPerMTok`）、比例字段无量纲、金额 `costCny/costUsd`（CostContext）；
- 能力分 ∈ [0,10]，任何写入方过 `caps_guard.validate`。
- **（v15.5 新增数据源契约）**：`model-tier-bridge.json`（档位枚举 + wire 拼写 + maxTokens）与 `model-pool.json`（成员名单）是能力档案的上游——桥文件由 host-bridge 插件生成（缺/坏文件时 Python 侧 fail-loud，不静默回退手填表）；池名单为成员准入第一层（不在池一律不参选）。

**时间口径**：全系统 `Asia/Shanghai`（`config_loader.now_shanghai`）；峰谷计价按工作日整点窗口；CFETS stale 按交易日历（周末/节假日保守标 stale，见 §7.1a）。

**失败处理契约**：
- 所有 python 入口退出码非 0 即失败；宿主 runPy 校验退出码/超时，失败写 `failed_runs.log` 并中止（run_council 抛错，绝不静默回退旧报告）；
- run 内失败（decompose 失败/任务超档位护栏 maxTaskTokens）写 result.json + 非 0 退出码（~~预算预检超限~~ v15.4 删除拒派路径）；
- 档案更新失败 → rounds.jsonl + result.json 双记录 + 工具输出告警；
- 文件写入一律 tmp+replace 原子替换；capabilities.json 写入前必须拿到文件锁（30s 超时 + 120s 死锁抢占）；
- 护栏事件日志失败不阻断选择（尽力而为），但事件五要素缺一不可。
