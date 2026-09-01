# Council 档位协议对齐方案（v15.5 Phase 0 扩充）

> 对应元评审问题 17-21（`meta-review-2026-08-25.md`）：插值废除（全档位×全案例）、max_tokens 还原模型上限、档位协议与 host-bridge 主会话对齐（单一数据源）。
> 状态：**方案细节（2026-08-25 maintainer 要求落盘），待审阅后动工**。

## 0. 问题与原则

**问题**：council 自建了两套手填参数（`benchmark/bench/config.py` 与 `orchestrator/config_loader.py`），与 host-bridge 主会话的档位协议不一致：
- council 给 DeepSeek 发 `budget_tokens`（512/4096/16384，两处还不一致）——老式思考 token 预算；
- host-bridge 主会话（host-bridge-llm-deepseek）发官方 `reasoning_effort`（off/low/high/max，off→`thinking:{type:'disabled'}`）——模型端自适应推理强度；
- pi-ai 系（MiniMax/OpenRouter）走 `thinkingLevelMap`（settings.yaml 的 `reasoningEfforts`：界面档位→wire 拼写）。
- maxTokens：council 拍 32768/8192/16384；host-bridge 侧权威值 = deepseek 默认 256000、ox-alpha 131072（settings.yaml 已配）、M3 继承 catalog 输出能力值。

**后果**：council 测的 `deepseek-v4-pro__high` ≠ 主会话用的 `deepseek-v4-pro high`——能力档案测量有效性受损，档案分数对主会话选档的参考价值打折。

**原则（maintainer 拍板）**：
1. **单一数据源**：档位枚举、wire 拼写、maxTokens 一律取 host-bridge 运行时模型目录（`ctx.llm.listModels` / `resolveModelInfo`），council 不再维护任何手填映射。
2. **测量与使用同源**：benchmark 测什么档位、council 跑什么档位，与主会话用的一致。
3. **全档位×全案例**：插值废除，每个模型按其 host-bridge 目录档位全量实测。
4. **max_tokens 还原模型上限**：不自定义截断值。

## 1. 插件桥（单一数据源机制）

**生成方**：`host-bridge plugin` 插件 `index.js`（宿主内，改动后重启 host-bridge 生效）。
**消费方**：Python orchestrator 与 benchmark（启动时读一次，缓存到 run 结束）。

### 1.1 桥文件 schema（`< user-data-dir >/model-tier-bridge.json`）

```json
{
  "schemaVersion": 1,
  "generatedAt": "2026-08-25T15:00:00+08:00",
  "source": "host-bridge-llm listModels/resolveModelInfo",
  "models": {
    "deepseek-v4-pro": {
      "provider": "deepseek-official",
      "contextWindow": 1048576,
      "defaultMaxTokens": 256000,
      "levels": [
        { "level": "off",  "wire": { "thinking": "disabled" } },
        { "level": "low",  "wire": { "thinking": "enabled", "reasoning_effort": "low" } },
        { "level": "high", "wire": { "thinking": "enabled", "reasoning_effort": "high" } },
        { "level": "max",  "wire": { "thinking": "enabled", "reasoning_effort": "max" } }
      ]
    },
    "MiniMax-M3": {
      "provider": "minimax-cn",
      "contextWindow": "<catalog>",
      "defaultMaxTokens": "<catalog 输出能力值>",
      "levels": [
        { "level": "<pi-ai 目录档位>", "wire": "<thinkingLevelMap 拼写>" }
      ]
    },
    "stealth/ox-alpha": {
      "provider": "openrouter",
      "contextWindow": 1048576,
      "defaultMaxTokens": 131072,
      "levels": [
        { "level": "low",  "wire": { "reasoning": { "effort": "low" } } },
        { "level": "high", "wire": { "reasoning": { "effort": "high" } } },
        { "level": "max",  "wire": { "reasoning": { "effort": "max" } } }
      ]
    }
  }
}
```

### 1.2 wire 拼写的来源与待确定点

- **DeepSeek**（已知，硬编码在桥生成器）：off→`thinking:{type:'disabled'}`；low/high/max→`thinking:{type:'enabled'}` + 官方顶层 `reasoning_effort` 同名值。来源 = host-bridge-llm-deepseek README（「low/high/max 以同名值序列化为官方顶层 reasoning_effort；off 序列化为 thinking.type: disabled」）。
- **OpenRouter/ox-alpha**（已知）：settings.yaml `reasoningEfforts` 已声明 low/high/max 原样透传 + reasoning mandatory（无 off 档，off 回落最低档 low）。council 现有 3 档条目（low/high/max）与之相符。
- **MiniMax-M3**（**待确定点 T1**）：M3 的档位枚举与 wire 拼写由 pi-ai 的 `thinkingLevelMap` 决定（对 minimax anthropic 接口的拼写）。实施时二选一：①插件在 host-bridge 进程内读取 pi-ai 包导出（`getSupportedThinkingLevels` + `thinkingLevelMap`），写入桥文件；②若 pi-ai 不导出拼写，对 M3 实测 API 接受的 thinking 参数形态（沿用 anthropic 接口的 thinking/budget 语义并逐档校准）。**方向明确：以 host-bridge 目录档位集合为准，council 现有 minimal/low/medium/high 五档中不在 host-bridge 目录的档位退役。**
- **maxTokens 注意**：resolveModelInfo 的 `defaultMaxTokens` 是「请求默认值」；模型输出**能力**值（catalog）可能更大。桥文件两者都带：`defaultMaxTokens`（请求默认）+ `capabilityMaxTokens`（能力上限）。benchmark 取能力上限（还原模型本身最大值，问题 18 语义），orchestrator 取 defaultMaxTokens（与主会话行为一致）。

### 1.3 桥文件刷新

- 插件启动时生成一次；`llm/adapters-updated` 事件（若有）时再生成；写文件走 tmp+replace 原子写。
- Python 侧启动时读桥文件；缺文件/坏文件 → 启动失败并显式报错（fail-loud，不静默回退手填表）。

## 2. Python 侧改造

| 文件 | 改动 |
|---|---|
| `benchmark/bench/config.py` | 删 `deepseek_thinking/minimax_thinking/openrouter_thinking/max_tokens_for` 四函数；CANDIDATES 改从桥文件生成（每模型 × 目录全档位）；`thinking_param(model, level)` 从桥文件取 wire |
| `orchestrator/config_loader.py` | 删 `thinking_param` 的 budget 映射与 `max_tokens_for`；改读桥文件 |
| `benchmark/bench/llm.py` + `orchestrator/stream_llm.py` | wire 序列化按桥文件（DeepSeek 发 `reasoning_effort`，pi-ai 系按拼写），max_tokens 按桥文件取 |
| `benchmark/bench/runner.py` | 响应文件追加 `thinking` 字段（问题 20，数据溯源） |
| `benchmark/build_capabilities.py` | 删 `interpolate`/`interp_map`/`MEASURED`；档位表从桥文件生成（问题 17）；删除后 4 个插值条目自然消失，全部条目 `interpolated:false` |

## 3. 旧成绩归档与档案重建

1. `benchmark/responses`、`benchmark/scores`、`scores-summary.json` → 整体移动到 `benchmark/archive/2026-08-25-pre-alignment/`（与 2026-08-24 归档同构）。
2. `capabilities.json` 现有 revision 快照备份（`.rev<N>.json.bak` 机制已有）。
3. 新协议全量重跑完成后：`build_capabilities.py` 重建档案（档位集合 = host-bridge 目录）；`capability_ingest` 摄入新成绩；pending diff 的 baseHash 同步（anchor 契约）。
4. 金标/考卷不受影响（金标考 judge 尺度，与档位协议无关）；judge_drift 基线不重建（judge 模型固定未变）。

## 4. 全档位×全案例重跑（新协议）

- 范围：所有模型 × host-bridge 目录全档位 × 37 题（9 维）。条目数变化：DeepSeek 每模型 4 档（off/low/high/max，low 从插值变实测）、M3 按 host-bridge 目录（待 T1）、ox 3 档。
- 成本重估：**¥15-25**（reasoning_effort 思考强度可能高于原 budget 档）+ M3 5h 额度（当前 65%，M3 部分跨窗口分批）。
- 时间：4-8 小时，断点续传 + 后台跑；跑完 `--only-failed` 补败。
- 验证：响应文件 thinking 字段有值；scores 覆盖全部档位×全部题；build_capabilities 产出无 `interpolated:true` 条目。

## 5. 实施顺序（并入 v15.5 Phase 0）

1. **0.2a 插件桥**：index.js 加桥文件生成（DeepSeek/ox 已知 wire + T1 解决 M3）→ 重启 host-bridge 验证桥文件内容与 `ctx.llm` 一致。
2. **0.2b Python 改造**：删手填映射、读桥文件、响应文件记 thinking 参数；pytest（config/loader 相关用例改写）全绿。
3. **0.3 归档旧成绩** → 4. **0.4 全量重跑**（后台）→ 5. **0.5 档案重建 + 校验**。
6. 之后才进入 Phase 1（判停标准）——避免在协议未对齐的档案上做判停实验。

## 6. 验证标准（maintainer 要求「验证通过」）

- pytest 全绿（新增：桥文件解析、wire 序列化、档位枚举一致性单测）。
- 桥文件内容与 `ctx.llm.listModels`/`resolveModelInfo` 逐字段一致（抽查 3 个模型）。
- 实测一发：桥文件 wire 直连 DeepSeek（reasoning_effort=high）与 host-bridge 主会话同档位请求的 usage/行为可比（reasoning token 量级一致）。
- 全量重跑后档案无 interpolated 条目；capabilities.json revision 递增、caps_guard 校验通过。
