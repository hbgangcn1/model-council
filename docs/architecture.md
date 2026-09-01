# Model Council 工作流程

> **⚠️ 已归档（2026-08-24）**：本文档描述旧流程（固定 reviewer 分工 + 静态表路由），仅作历史参考。
> 现行工作流以 `DESIGN-v14.md`（v15.1）为准：decompose（维度权重向量）→ selector 动态分配 → 交叉执行/交叉验证收敛循环 → 自进化回写。

## 概述

Model Council 是一个 cost-aware 多模型协作系统。核心思想：**把一个复杂请求拆成多个子任务，分配给评测分数最高的模型并行执行，再通过验证、交叉审查、合成产出最终报告。**

相比旧流程（所有模型看同一份 brief），新流程实现了：
- 任务拆分：每个 reviewer 做不同的子任务
- 数据驱动：模型选择基于 10 模型 × 18 用例的 benchmark 评测
- 验证层：Verifier 5 维评分检查输出质量
- 成本优化：80-90% 的 council 工作由免费/低价模型完成

---

## 流程总览

```
[请求] → [Router] → [Decomposer] → [Phase 1: 并行子任务] → [Phase 2: 验证]
    → [Phase 3: 交叉审查] → [Phase 4: 合成] → [Phase 5: 裁判] → [报告]
```

---

## 第 1 步：Router 分类

收到请求后，用 `select-council.mjs` 对 9 个维度打分（0-5），自动选择 preset：

| 维度 | 说明 |
|---|---|
| complexity | 任务复杂度 |
| reversibilityRisk | 决策可逆程度 |
| costOfWrongDecision | 错误决策的成本 |
| moneyRisk | 涉及金钱/订阅的风险 |
| securityPrivacyRisk | 安全/隐私风险 |
| implementationCode | 是否需要写代码 |
| longContext | 是否需要长上下文 |
| capabilityChangingConfig | 是否改变系统能力 |
| important | 是否重要决策 |

### Preset 路由规则

| 条件 | Preset | 说明 |
|---|---|---|
| 以上都不满足 | `lite` | 日常建议 |
| 需要写代码 | `code` | 工程/调试/代码审查 |
| 复杂工程 (complexity ≥ 4) | `code-plus` | 多步实现 |
| 需要长上下文 | `long` | 长文档/日志/repo |
| complexity ≥ 4 或重要 | `balanced` | 重要但非高风险 |
| 高风险/不可逆/安全 | `premium` | 需要 GPT-5.5 挑战 + Fable-5 裁决 |
| 最高风险 | `supreme` | 所有输出需 Fable-5 批准 |

---

## 第 2 步：Decomposer 拆分（balanced 及以上）

**模型**：MiniMax-M2.7（免费，快速规划）

Decomposer 分析请求，输出 JSON 拆分计划：

```json
{
  "complexity": 4,
  "category": "research",
  "subtasks": [
    {
      "id": "subtask-1",
      "title": "市场规模与增长趋势",
      "description": "研究目标市场的当前规模、历史增长率、未来预测。需要具体数字和来源。",
      "preferredModels": ["opencode-go/qwen3.6-plus"],
      "reason": "需要长上下文处理大量市场报告"
    },
    {
      "id": "subtask-2",
      "title": "竞品技术架构对比",
      "description": "对比 3-5 个竞品的技术栈、架构设计、优劣势。",
      "preferredModels": ["opencode-go/deepseek-v4-pro"],
      "reason": "需要深度技术分析"
    }
  ],
  "synthesisNotes": "重点关注 subtask-1 和 subtask-2 的交叉验证"
}
```

### 模型分配逻辑

对每个 subtask，按以下顺序选择模型：

1. **preferredModels 优先**：用 Decomposer 指定的 `preferredModels[0]`（前提是模型可用且未被其他 subtask 占用）
2. **Preset 模型池 fallback**：如果 preferred 全不可用，从 preset 的模型列表按顺序选未使用的
3. **最终 fallback**：MiniMax-M2.7

`usedModels` Set 跨 subtask 累积，确保同一模型不会被分配给多个 subtask。

### Decomposer 选模型的依据（Benchmark 数据）

Decomposer 在选择 `preferredModels` 时参考 2026-05-19 的评测结果：

| 模型 | 总分 | 最强维度 | 最弱维度 | 成本 |
|---|---|---|---|---|
| **glm-5.1** | 98% | 全满分（除 code 90%） | code | $0.02 |
| **qwen3.6-plus** | 98% | 全满分（除 code 90%） | code | $0.02 |
| deepseek-v4-pro | 97% | chinese/reasoning/instruction 100% | code 87% | $0.02 |
| mimo-v2.5-pro | 97% | instruction/long_context/reasoning 100% | code 90% | $0.02 |
| xiaomi mimo | 97% | 同上 | code 90% | 免费 |
| kimi-k2.6 | 96% | long_context 100% | code 87% | $0.02 |
| MiniMax-M2.7 | 91% | reasoning 100% | instruction 80% | 免费 |
| GPT-5.5 | 98% | 全满分 | code 90% | $0.50 |
| Fable-5 | 96% | instruction/research 100% | code 87% | $1.00 |
| Gemini 3.1 Pro | 95% | chinese/research 100% | long_context 75% | $0.10 |

---

## 第 3 步：Phase 1 — 并行子任务执行

每个 subtask spawn 一个独立的 subagent：

- **每个 reviewer 只拿到自己的子任务**（不是完整请求）
- 使用 `lightContext: true` 减少开销
- 使用 `mode: "run"`, `cleanup: "keep"` 保留输出
- `runTimeoutSeconds: 3600`（不限制单次调用超时）
- 使用绝对 Windows 路径写入 run artifacts

同时 spawn 一个 **watchdog subagent**：
- 每 30 秒检查 `.done` 文件
- 如果 reviewer 超过 150 秒没完成，写 `.timeout` 标记
- 必须有 `read` + `exec` 权限（不能只读）

### Phase 1 输出格式

每个 reviewer 按以下格式输出：

```markdown
## 子任务
[子任务标题]

## 核心发现
- ...（3-5 条关键发现，每条附证据/来源）

## 结论
[针对子任务的明确回答]

## 置信度
0.0-1.0，一句话说明

## 关键假设
- ...

## 局限性
- ...

## 建议下一步
- ...
```

输出控制在 1200 中文字符以内。

---

## 第 4 步：Phase 2 — 验证

**模型**：glm-5.1（评测最高分 98%）

Verifier 收到：原始请求 + 所有 reviewer 的子任务输出。

对每个子任务输出打 5 个维度的分（0-5）：

| 维度 | 检查内容 |
|---|---|
| 事实一致性 | 结论有数据支撑吗？有无来源的断言？数字/日期/名称可信？ |
| 逻辑一致性 | 推理链完整？自相矛盾？前提→结论推导成立？ |
| 交叉验证 | 与其他子任务矛盾？矛盾时谁更可信？ |
| 完整性 | 回答了子任务所有要求？遗漏重要维度？ |
| 可操作性 | 建议具体可执行？有没有"需要进一步研究"的废话？ |

### 验证输出格式

```markdown
## 验证报告

### 总体质量评级
- 平均分: X.X / 5.0
- 最高质量: subtask-X (X.X)
- 最低质量: subtask-X (X.X)

### 各子任务详细评分
（每个子任务一个表格：维度/分数/说明）

### 发现的关键问题
1. **[严重]** ...（需要在合成前修正）
2. **[中等]** ...（合成时需要注意）
3. **[轻微]** ...（可以接受但需标注）

### 交叉矛盾
- subtask-1 说 X，subtask-2 说 Y → 建议：...

### 对合成器的建议
- 优先采用哪些结论（高置信度）
- 需要降权的结论（低置信度或有争议）
```

---

## 第 5 步：Phase 3 — 交叉审查

重新 spawn 每个 reviewer 做交叉审查。每个 reviewer 读到的是**其他 reviewer 的子任务输出**。

```markdown
## 交叉审查

### 我的子任务摘要
（一句话）

### 对其他子任务的标注

#### [subtask-1 标题]
- **立场**：同意 / 反对 / 需查证
- **理由**：（2-3 句话）

### 核心冲突
1. ...

### 建议解决方向
（一句话）
```

**只做一轮**，不做多轮辩论（避免超时风险）。输出控制在 600 中文字符以内。

---

## 第 6 步：Phase 4 — 合成

**模型**：qwen3.6-plus（评测最高分 98%）

Synthesizer 收到：原始请求 + Phase 1 输出 + 验证报告 + 交叉审查结果。

### 合成输出格式

```markdown
## 结论
[一句话直接回答]

## 使用级别与模型
[preset 和模型列表]

## 子任务发现汇总

### [子任务 1 标题]
- **核心结论**：...
- **置信度**：X.X
- **关键证据**：...

### [子任务 2 标题]
- **核心结论**：...
- **置信度**：X.X
- **关键证据**：...

## 模型共识
- ...

## 主要分歧
- ...

## 验证器指出的问题
- ...

## 推荐方案
[综合所有子任务发现后的明确建议]

## 风险
1. ...

## 执行步骤
1. ...

## 验证方法
- ...

## 回滚方案
- ...

## 成本估算
- ...
```

---

## 第 7 步：Phase 5 — 裁判（premium/supreme only）

### premium preset

1. **GPT-5.5** 做 adversarial challenger — 专门找漏洞、缺失证据、逻辑缺陷
2. **Fable-5** 做 final judge — 最终裁决

### supreme preset

同 premium，但所有输出需 Fable-5 批准才能用。

### balanced 及以下

跳过裁判，合成器输出直接发给用户。

---

## 第 8 步：输出

1. 报告写入 `model-council/reports/YYYY-MM-DD-topic.md`
2. 追加 ledger JSONL 到 `model-council/ledger/YYYY-MM.jsonl`
3. Trace 把报告发给用户
4. 不自动执行 capability-changing 操作（需用户确认）

---

## 完整流程图

```
[你的请求]
    ↓
[Router 打分] → 选 preset (lite/code/balanced/premium/supreme)
    ↓
┌───────────────────────────────────────────────┐
│ balanced 及以上：走 Decomposer 流程            │
│ lite/code：跳过 Decomposer，旧流程            │
└───────────────────────────────────────────────┘
    ↓
[Decomposer 拆分子任务]  ← MiniMax (免费)
    ↓
┌──────────┬──────────┬──────────┐
↓          ↓          ↓          ↓
subtask-1  subtask-2  subtask-3  watchdog
qwen       deepseek   glm        MiniMax
(98%)      (97%)      (98%)      (免费)
└──────────┴──────────┴──────────┘
    ↓
[Verifier 验证]  ← glm (98%)
    5 维评分：事实/逻辑/交叉/完整/可操作
    ↓
[交叉审查]  ← 每个 reviewer 互看
    同意/反对/需查证 + 核心冲突
    ↓
[Synthesizer 合成]  ← qwen (98%)
    结构化报告：结论/发现/共识/分歧/风险/步骤
    ↓
[Challenger + Judge]  ← GPT-5.5 + Fable-5 (premium/supreme only)
    ↓
[报告]
```

---

## 模型分配总表

| 角色 | 模型 | 成本 | 何时使用 |
|---|---|---|---|
| Decomposer | MiniMax-M2.7 | 免费 | balanced 及以上 |
| Subtask Reviewer | glm-5.1 / qwen3.6-plus | $0.02 | 所有 council |
| Verifier | glm-5.1 | $0.02 | balanced 及以上 |
| Synthesizer | qwen3.6-plus | $0.02 | 所有 council |
| Challenger | GPT-5.5 | $0.50 | premium/supreme |
| Judge | Fable-5 | $1.00 | premium/supreme |

---

## 成本对比

| | 旧流程 | 新流程 |
|---|---|---|
| **balanced council** | 3-4 个模型，含 GPT/Fable-5 → ~$2 | 4 个 Tier 2 模型 → ~$0.08 |
| **premium council** | 7 个模型，含 GPT/Fable-5 → ~$5 | 4 个 Tier 2 + GPT + Fable-5 → ~$1.60 |
| **supreme council** | 7 个模型，含 GPT/Fable-5 → ~$5 | 同 premium |
| **预计成本降低** | — | **80-90%** |

---

## 关键约束

1. **不自动执行 capability-changing 操作**：Council 推荐的方案需要用户确认
2. **code 维度需要额外验证**：所有模型 code 评分 87-90%，代码类子任务必须交叉验证
3. **只做一轮交叉审查**：多轮辩论超时风险太高
4. **超时处理**：reviewer 超时 → 用 fallback 模型重试一次 → 仍失败则在合成时标注缺失
5. **watchdog 独立运行**：避免主 session 的 push-vs-pull 死锁
6. **⚠️ 不依赖 announce/completion event**：subagent 完成后会自动 announce 回主 session，触发 session-write-lock 竞争。所有 reviewer 必须通过**写文件**来交付结果，主 session 通过**轮询文件**（检查 `.done` 标记）来获取结果。详见 SKILL.md 的「反 announce 机制」。

---

## 文件结构

```
model-council/
├── WORKFLOW.md              ← 本文档
├── policy.md                ← 路由、安全、审批规则
├── presets.json             ← 模型列表和 preset 配置（含 domainPresets）
├── pricing.json             ← 定价假设
├── thinking-profiles.json   ← 模型 thinking level 配置
├── cost-tiers.json          ← 成本分层
├── usage.json               ← 用量追踪
├── prompts/
│   ├── decomposer.md        ← 任务拆分 agent prompt
│   ├── reviewer.md          ← 子任务 reviewer prompt
│   ├── verifier.md          ← 验证 agent prompt
│   ├── synthesizer.md       ← 合成 agent prompt
│   ├── challenger.md        ← 对抗 challenger prompt
│   ├── judge.md             ← 裁判 judge prompt
│   ├── supreme-judge.md     ← 最高裁判 prompt
│   ├── cross-reviewer.md    ← 交叉审查 prompt
│   └── router.md            ← 路由启发式
├── tools/
│   ├── select-council.mjs   ← 路由 + 模型分配脚本
│   ├── retry-handler.mjs    ← 超时重试处理
│   └── track-usage.mjs      ← 用量追踪
├── benchmarks/
│   ├── capabilities.json    ← 10 模型能力档案
│   ├── cases/               ← 18 个测试用例（6 维度）
│   ├── results/             ← 各模型原始输出
│   ├── scores/              ← 评分结果
│   └── run.mjs              ← 评测运行器
├── reports/                 ← Council 报告
├── ledger/                  ← 执行记录
├── evals/                   ← 模型评分
└── runs/                    ← 历史 run 数据
```

---

## 相关文档

- [SKILL.md](~/.agents/skills/model-council/SKILL.md) — OpenClaw skill 定义
- [policy.md](policy.md) — 详细路由和安全规则
- [benchmarks/capabilities.json](benchmarks/capabilities.json) — 模型能力档案
