# Model Council

> Self-evolving, cost-aware multi-model convergent review system.

[中文版](#中文版) | [English](#english)

> **分发**：本仓库即源头——**不发布到 PyPI**。
> 朋友想跑就 git clone 后 `pip install -e .`（见 [安装](#安装从源码)）。

> **Distribution**: this repository is the source of truth — **not** published to PyPI.
> Friends who want to run it clone the repo and `pip install -e .` (see [Install](#install-from-source)).

---

## English

**Model Council** is a Python library that runs a panel of LLM models against a task,
**converges** them through a round-robin of cross-execution and cross-verification,
and synthesizes a high-confidence answer. Capabilities of every model × thinking-level
combination are tracked in a JSON file (`capabilities.json`) that the **selector**
consults on every run to pick the best model for each role (executor / verifier /
synthesizer / decomposer).

The whole system **self-evolves**: every run writes runtime feedback to a JSONL stream,
and a nightly job rolls those signals back into the capability archive under
maintainer-approved pending diffs.

### Why

- **Single-model answers are unreliable.** Cross-verification by an independent
  model catches factual / logical errors before they reach you.
- **Capability ≠ Price.** Different models excel at different dimensions (code /
  Chinese / reasoning / research / ...). Cost-aware selection picks the cheapest
  model that meets each subtask's needs.
- **Capability drifts over time.** A model that scored 9.5 last month may score 8.0
  this month. The selector's nightly self-evolve loop catches this without you
  babysitting it.
- **No vendor lock-in.** The selector's vendor-group constraint guarantees every
  cross-check is between **different** vendors.

### What it does

```
                  ┌──────────────────────────────────────┐
                  │              run_council             │
                  │   (decompose → exec × N → verify × N │
                  │      → converge → synthesize)        │
                  └──────────────────────────────────────┘
                                    │
            ┌───────────────────────┼───────────────────────┐
            ▼                       ▼                       ▼
    ┌──────────────┐        ┌──────────────┐        ┌──────────────┐
    │  selector.py │        │ benchmark/   │        │   config/    │
    │  (per-task   │        │ (capability  │        │ (params,     │
    │   model pick)│        │   archive)   │        │  pricing)    │
    └──────────────┘        └──────────────┘        └──────────────┘
            │                       │
            ▼                       ▼
    ┌──────────────────────────────────────────┐
    │   LLMClient (pluggable transport)        │
    │   • subprocess client (default)          │
    │   • HTTP client (OpenAI-compat)          │
    │   • host-bridge adapter (optional)        │
    └──────────────────────────────────────────┘
```

### Install (from source)

```bash
git clone https://github.com/hbgangcn1/model-council.git
cd model-council
pip install -e .
```

> This project is **not** published to PyPI. Install from a git clone.

### Configure

Model Council needs:
1. **At least 2 LLM providers** with API keys (cross-vendor verification).
2. A `capabilities.json` archive (start empty, run benchmark to populate).
3. Optionally a pricing profile.

See `config/council-params.example.json` for tunable parameters and `docs/configuration.md`
for the full setup guide.

### Run

```bash
# Inline mode (concise, for quick checks; ~5-15 minutes)
model-council --task "Review this design decision" --tier fast --mode inline

# Report mode (full markdown report with evidence chain; ~15-30 minutes)
model-council --task "Should we migrate from X to Y?" --tier standard --mode report

# Deep tier (irreversible decisions; ~30-60 minutes, dual-execution cross-review)
model-council --task "Should we delete the legacy user table?" --tier deep --mode report
```

Or programmatically:

```python
from orchestrator.council_v14 import run_council

result = run_council(
    task="Compare PostgreSQL vs MongoDB for our use case",
    tier="standard",
    mode="report",
)
print(result["report"])  # path to report.md
print(result["status"])  # "converged" / "early_stop" / "stalled" / "forced"
```

### Benchmark

Before using the selector on real tasks, populate `capabilities.json` with measurements
of every model × thinking-level combination you have:

```bash
# Run full benchmark (1-2 hours, ~$15-25 depending on models)
python -m benchmark.bench.runner --cases benchmark/v21-cases.json --output benchmark/scores/

# Ingest benchmark results into the capability archive (with maintainer approval)
python -m benchmark.capability_ingest --diff    # preview
python -m benchmark.capability_ingest --apply   # apply after approval
```

### Self-evolve

Every run writes one feedback row to `runtime-feedback.jsonl`. A nightly job rolls
those into pending changes to the capability archive. The apply step is gated by
a drift health check; only healthy diffs auto-apply.

```bash
# Run the nightly self-evolve manually (normally scheduled)
python -m orchestrator.update_capabilities --check      # health check only
python -m orchestrator.update_capabilities --pending    # build pending diff
python -m orchestrator.update_capabilities --apply      # apply pending (gated)
```

### Architecture highlights

- **Capability-driven selection**: every `model@thinking` has a 9-dimensional score
  (reasoning / code / chinese / research / instruction_following / long_context /
  tool_use / creativity / safety). Selector picks by
  `Σ dim_weight × capability − λ·cost − μ·latency + diversity`.
- **Hard invariants**: cross-vendor verification (`vendorGroup`), self-verify ban
  (verifier ≠ executor), capability revisions with `baseHash` integrity, runway
  circuit breaker with half-open probing.
- **Cost philosophy**: cost selects among equivalent-capability models; it does
  *not* cap usage. Runaway cost is bounded by max-rounds and wall-clock, not by
  budget termination (v15.4 design decision).
- **JSON repair**: every model output goes through a 4-stage lenient parser
  (fence strip / bare-newline state machine / truncation repair / unescaped quote
  heuristic), with diagnostic retry and substitute-verifier escalation.
- **Audit trail**: every run writes `task.md`, `decisions.jsonl`, `rounds.jsonl`,
  `cost.jsonl`, `budget.jsonl`, `verdict-raw/`, `result.json`, `report.md`. No silent
  fallbacks.

### What's NOT in this repo

- Your `capabilities.json`, `balance-snapshot.json`, `circuit-state.json` — those
  live in your data dir (default `~< user-data-dir >/`) and contain your model registry,
  account balances, and runtime telemetry. Never commit them.
- Historical runs (`runs/`), reports (`reports/`), evidence (`evidence/`),
  ledger (`ledger/`), legacy OpenClaw-era tooling (`tools/`) — those accumulate over
  time on your local machine. They are **not** part of the library.
- The DeepSeek Harness (DSH) bridge plugin (lives in
  `~/.dsh/profiles/web/node_modules/host-bridge plugin/`) — that is a separate integration
  project.

### License

MIT — see `LICENSE`.

---

## 中文版

**Model Council** 是一个 Python 库，用 LLM 模型小组评审任务，通过交叉执行 + 交叉验证
的迭代收敛，合成高置信度答案。每个模型 × 思考档位的组合能力都记录在 `capabilities.json`
档案里，`selector.py` 每次跑任务都根据它挑选最合适的模型（执行者 / 验证者 / 综合者 / 分解者）。

整个系统**自进化**：每次跑都把运行期反馈写入 JSONL 流，每晚把这些信号回写到能力档案
（受维护者审批的 pending diff 控制）。

### 为什么

- **单模型答案不可靠**。独立模型交叉验证在事实 / 逻辑错误到达你之前就抓住它们。
- **能力 ≠ 价格**。不同模型在不同维度（代码 / 中文 / 推理 / 研究 / ...）有强弱。
  成本感知的选择为每个子任务挑最便宜的「够用」模型。
- **能力会随时间漂移**。上个月 9.5 分的模型这个月可能 8.0。selector 的每晚自进化
  循环自动捕捉漂移，你不用盯着。
- **无厂商锁定**。selector 的 vendorGroup 约束保证每次交叉验证都来自**不同**厂商。

### 它做什么

```
                  ┌──────────────────────────────────────┐
                  │              run_council             │
                  │   (decompose → exec × N → verify × N │
                  │      → converge → synthesize)        │
                  └──────────────────────────────────────┘
                                    │
            ┌───────────────────────┼───────────────────────┐
            ▼                       ▼                       ▼
    ┌──────────────┐        ┌──────────────┐        ┌──────────────┐
    │  selector.py │        │ benchmark/   │        │   config/    │
    │  (per-task   │        │ (capability  │        │ (params,     │
    │   model pick)│        │   archive)   │        │  pricing)    │
    └──────────────┘        └──────────────┘        └──────────────┘
            │                       │
            ▼                       ▼
    ┌──────────────────────────────────────────┐
    │   LLMClient (pluggable transport)        │
    │   • subprocess client (default)          │
    │   • HTTP client (OpenAI-compat)          │
    │   • host-bridge adapter (optional)        │
    └──────────────────────────────────────────┘
```

### 安装（从源码）

```bash
git clone https://github.com/hbgangcn1/model-council.git
cd model-council
pip install -e .
```

> 本项目**不发布到 PyPI**，从 git 克隆安装。

### 配置

Model Council 需要：
1. **至少 2 个 LLM provider** 的 API key（跨厂商验证）
2. 一份 `capabilities.json` 档案（从空开始，跑 benchmark 填充）
3. 可选的价格档案

详见 `config/council-params.example.json` 和 `docs/configuration.md`。

### 运行

```bash
# inline 模式（简洁快速，5-15 分钟）
model-council --task "评审这个设计决策" --tier fast --mode inline

# report 模式（完整报告 + 证据链，15-30 分钟）
model-council --task "我们应该从 X 迁移到 Y 吗？" --tier standard --mode report

# deep 档（不可逆决策，30-60 分钟，双路交叉执行）
model-council --task "应该删除遗留用户表吗？" --tier deep --mode report
```

或编程调用：

```python
from orchestrator.council_v14 import run_council

result = run_council(
    task="对比 PostgreSQL 和 MongoDB 在我们场景下的选择",
    tier="standard",
    mode="report",
)
print(result["report"])  # report.md 的路径
print(result["status"])  # "converged" / "early_stop" / "stalled" / "forced"
```

### 跑分

真实用 selector 之前，先用 benchmark 填充 `capabilities.json`：

```bash
# 跑全量 benchmark（1-2 小时，依模型不同 $15-25）
python -m benchmark.bench.runner --cases benchmark/v21-cases.json --output benchmark/scores/

# 把跑分结果摄入能力档案（维护者审批后）
python -m benchmark.capability_ingest --diff    # 看 diff
python -m benchmark.capability_ingest --apply   # 审批后合入
```

### 自进化

每次 run 写一行反馈到 `runtime-feedback.jsonl`。每晚把这些回滚到能力档案的 pending diff。
apply 步骤由漂移体检门控，只有健康的 diff 才会自动 apply。

```bash
# 手动跑每晚自进化（通常是定时任务）
python -m orchestrator.update_capabilities --check      # 只体检
python -m orchestrator.update_capabilities --pending    # 构建 pending diff
python -m orchestrator.update_capabilities --apply      # 合入（受门控）
```

### 架构亮点

- **能力驱动选择**：每个 `model@thinking` 有 9 维分数（推理 / 代码 / 中文 / 研究 /
  指令遵循 / 长上下文 / 工具调用 / 创造性 / 安全）。selector 按
  `Σ dim_weight × capability − λ·cost − μ·latency + diversity` 挑选。
- **硬约束**：跨厂商验证（vendorGroup）、自验禁令（verifier ≠ executor）、能力档案
  revision 带 baseHash 完整性、跑道熔断器带半开探测。
- **成本哲学**：成本只在能力相当的模型间做选择，**不**做用量控制。失控靠 max-rounds
  和墙钟兜底，不靠预算强制终止（v15.4 设计决议）。
- **JSON 修复**：每个模型输出走 4 阶段 lenient 解析器（fence 剥离 / 裸换行状态机 /
  截断修复 / 未转义引号启发式），含诊断重试和替补验证者升级。
- **审计链**：每次 run 写 `task.md`、`decisions.jsonl`、`rounds.jsonl`、`cost.jsonl`、
  `budget.jsonl`、`verdict-raw/`、`result.json`、`report.md`。绝不静默回退。

### 这个仓库**不**包含什么

- 你的 `capabilities.json`、`balance-snapshot.json`、`circuit-state.json`——它们
  在你的数据目录（默认 `~< user-data-dir >/`），含你的模型注册、账户余额、运行期遥测。
  **绝不提交**。
- 历史 runs (`runs/`)、reports (`reports/`)、evidence (`evidence/`)、ledger (`ledger/`)、
  旧 OpenClaw 时代工具 (`tools/`)——这些在你本地累积，**不**属于本库。
- DSH（DeepSeek Harness）桥插件（在 `~/.dsh/profiles/web/node_modules/host-bridge plugin/`）——
  那是单独的集成项目。

### 许可证

MIT — 见 `LICENSE`。