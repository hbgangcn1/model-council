# Model Council 架构总览

> 现行架构（v15.9，与代码一致）。English: see `operations.md` §0/§9 for the
> subsystem map and the DSH-vs-public boundary.
> 历史注脚：2026-05 的固定分工 + preset 路由体系（`select-council.mjs`、
> lite/code/balanced/premium/supreme、watchdog 子代理）已随 v14 退役，实现被
> 下述动态机制取代，旧文可在 git 历史中查阅。本文只描述现在。

## 一句话

复杂请求 → 拆成带维度权重的子任务 → 每次按能力档案动态选模型
（执行/验证/综合，跨厂商、禁自验）→ 交叉执行 + 交叉验证逐轮收敛
（θ/边际早停/停滞/轮数上限）→ 合成报告 → 运行期反馈回写 JSONL →
每晚门禁落盘进化档案。

## 入口与档位

```bash
model-council --task "…" --tier fast|standard|deep --mode report|inline
python -m orchestrator.council_v14 --task "…" --tier fast   # 同上
```

```python
from orchestrator.council_v14 import run_council
result = run_council(task="…", tier="standard", mode="report")
# result: {"status", "run_dir", "rounds", "s_history", "report", …}
```

- `fast`（日常低风险）/ `standard`（重要默认）/ `deep`（高风险不可逆，
  双路执行互评）。`inline` 结论即回复，`report` 落完整报告 + 审计。
- 每次 run 落 `runs/<时间>/`：`task.md`、`decisions.jsonl`（为什么选它）、
  `rounds.jsonl`（各轮评分 + 终止审计）、`cost.jsonl`（估算/预算/实际三口径）、
  `budget.jsonl`（余额预检）、`verdict-raw/`、`result.json`、`report.md`。

## 收敛循环

```
decompose（维度权重向量）
  → selector.select（Σ维度权重×能力分 − λ×成本 − μ×延迟 + 多样性，6 道护栏）
  → 交叉执行 × 交叉验证（verifier 跨厂商、禁自验、执行者轮换）
  → Verifier 打分 + rework 清单 → 终止判定 →（不达标）增量返工
  → synthesize → 双输出 → 运行期反馈（verifier 分/rework/可靠性）→ JSONL
```

终止判据（`terminator.py`）：θ=9.5 三档统一（理论天花板，极少达到）/
δ 边际早停 / 同问题两轮 stalled / 档位 maxIter + max_rounds 防呆 +
墙钟与预算双保险。多数 run 以 early_stop 收尾——预期内，不是失败。

## 模型接入（LLMClient）

`orchestrator` 经 `LLMClient` 协议调模型，transport 可插拔
（`orchestrator/llm_client.py`，ADR-004）：

| Transport | 给谁用 |
|---|---|
| DSH bridge（`/api/council/llm-stream`，pi-ai 处理各家差异） | DSH 内用户，默认 |
| OpenAI-compat HTTP | `OPENAI_COMPAT_BASE` + KEY |
| Anthropic-compat HTTP | `ANTHROPIC_COMPAT_BASE` + KEY |
| Stub | 配错时给明确报错，不静默 |

错误语义：额度用尽（`LLMQuotaExhaustedError`）零重试直接上抛；
429/5xx 退避重试；成功/失败回调进 selector 熔断器。档位 wire 拼写与
max_tokens 取自 `tier_bridge` 单一源（公开版为 family 默认实现，详见
`tier-alignment.md` 状态说明）。

## 成本（ADR-001）

钱只在**同等能力**的模型之间选，不做用量上限。防失控靠最大轮数 +
墙钟 + 余额烧空。记账货币 CNY，汇率每日 09:30 三级 fallback 更新；
余额/额度快照 60 秒缓存。详见 `operations.md` §6/§8。

## 目录（真实结构）

```
model-council/
├── orchestrator/       # 收敛循环 + selector + 成本/汇率/余额/judge/落盘
│   ├── council_v14.py  # 唯一 orchestrator 入口（别被同目录 council.py 误导）
│   ├── llm_client.py + llm_transports/   # 传输抽象与四 transport
│   ├── test_*.py       # 离线回归（pytest）
│   └── audit_council_orchestrator.py     # 入口审计：改架构前先跑它
├── benchmark/          # 跑分管线 + 金标集 + 摄入审批 + 自进化
├── config/             # *.example.json 参数/价格模板
├── docs/               # 本目录（operations.md 是运维入口）
├── scripts/            # first_run.py / first-bench.sh / sanitize_snapshot.py
├── tests/              # pytest 风格测试
├── capabilities.json   # 脱敏启动快照（你的实时改动不提交，见 operations §2）
└── council-params.json # 全部开关（`params.py --show` 查看）
```

源码/排气二分法：定时任务写出来的东西（runs/、快照、事件流、scores/）
都不是源码，不提交。运维整环见 `operations.md`。

## 审计命令

改架构、加模型前先跑，防误判入口与配置中心（v15.6 L3 教训）：

```bash
cd <repo> && python audit_council_orchestrator.py
```
