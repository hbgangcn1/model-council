# Operations Manual（运维手册）

> v15.9 · English first, [中文版](#中文版运维手册) below.
> Companion: [configuration questions → this manual](#data-files-who-writes-what).
> The old `docs/configuration.md` link from README now lands here.

Model Council is not a script you run — it is a system you keep. A single
council run is 10% of the value; the other 90% is the daily loop that keeps
capabilities fresh, costs honest, judges calibrated and benchmarks relevant.
This manual covers the full loop: what runs when, what each piece costs,
what breaks if you skip it, and how to fix the five failures you will
actually see.

Contents: [0. System map](#0-system-map) · [1. Daily ops chain](#1-daily-ops-chain)
· [2. Data files](#2-data-files-who-writes-what) · [3. Self-evolve gate](#3-self-evolve-apply-gate)
· [4. Judge baselines](#4-judge-baseline-management) · [5. Model lifecycle](#5-model-lifecycle)
· [6. Balance & FX](#6-balance--fx) · [7. Troubleshooting](#7-troubleshooting)
· [8. Cost cheat sheet](#8-cost-cheat-sheet) · [9. DSH-only vs public](#9-dsh-only-vs-public)
· [10. Release checklist](#10-release-checklist-maintainer)

## 0. System map

| # | Subsystem | If you ignore it | Local (DSH) | Public |
|---|---|---|---|---|
| 1 | Council run (`council_v14.py`) | — (the entry point) | tool / console / CLI | `python -m orchestrator.council_v14 --task …` |
| 2 | Benchmark + ingest (`bench/runner.py`, `capability_ingest.py`) | selector picks blind | console button / CLI | `./scripts/first-bench.sh`, `--diff`/`--apply` |
| 3 | Daily ops chain (4 jobs) | archive rots, drift lies | task-panel, automatic | cron + commands below (§1) |
| 4 | Balance/quota (`query_balance.py`) | quota burnouts surprise you | console + 60s snapshot | same script, needs keys (§6) |
| 5 | FX rates (`fetch_exchange_rate.py`, `fx_status.py`) | CNY accounting halts | 09:30 auto | same scripts (§6) |
| 6 | Self-evolve apply (`update_capabilities.py`) | feedback piles up, never lands | auto + console | `--check/--pending/--apply` (§3) |
| 7 | Judge drift + baseline (`judge_drift.py`) | silent judge drift | nightly auto | same script (§4) |
| 8 | Golden evolve (`golden_evolve.py --fill-one`) | gold set stagnates | nightly auto | same script |
| 9 | Gold anchor review | bad gold poisons drift signal | weekly push to maintainer (DSH-only) | manual: re-read 5 random items/month |
| 10 | Params (`params.py --show`) | magic numbers | console | `python -m orchestrator.params --show` |
| 11 | Cost reconcile (`cost_calibrate.py --check`) | estimates drift from reality | 04:00 auto | same script |
| 12 | Observability (SLA, guardrails, `/metrics`) | flying blind | console + `/metrics` | `sla_stats.py`, event JSONLs |
| 13 | Model retire/anchor (`retire_candidate.py`, `anchor_candidate.py`) | dead models linger | console + scripts | scripts (`--help`) |
| 14 | Release (`sanitize_snapshot.py`, CHANGELOG) | public rots | maintainer only | §10 |

## 1. Daily ops chain

Four jobs, in this order. Times are the maintainer's (Asia/Shanghai); pick
your own, keep the order and gaps (each job reads the previous one's output).

| Time | Command | Cap | Exit codes | Skip consequence |
|---|---|---|---|---|
| 02:00 | `python -m benchmark.auto_evolve` | 7200s | 0 ok; non-0 fail — but first read `auto-evolve-state.json`: `paused:true` means the fuse tripped, **do not** force-run | benchmark/cases drift from reality; stale cases keep burning money |
| 04:00 | `python -m orchestrator.cost_calibrate --check` | 120s | 0 in-threshold; **2 = drift over threshold (alert, not failure)** | estimates silently diverge from actuals |
| 04:30 | `python -m orchestrator.judge_drift` → `python -m orchestrator.update_capabilities --apply` → `python -m benchmark.golden_evolve --fill-one` | 1500s / 120s / 600s | judge: 0 ok, 2 drift-alert, 1 fail; `QUOTA_EXHAUSTED` in output = skip everything, retry nothing | judge drift + pending feedback never land; gold set stagnates |
| 09:30 | `python -m orchestrator.fetch_exchange_rate` | 120s | 0 ok (CFETS → fallback API → last good rate) | FX goes stale; CNY accounting halts after `staleHaltDays` trading days (see `fx_status.py`) |

cron example (Linux; logs are your audit trail):

```cron
0 2 * * * cd /path/to/model-council && python -m benchmark.auto_evolve >> ops.log 2>&1
0 4 * * * cd /path/to/model-council && python -m orchestrator.cost_calibrate --check >> ops.log 2>&1
30 4 * * * cd /path/to/model-council && python -m orchestrator.judge_drift >> ops.log 2>&1 && python -m orchestrator.update_capabilities --apply >> ops.log 2>&1 && python -m benchmark.golden_evolve --fill-one >> ops.log 2>&1
30 9 * * * cd /path/to/model-council && python -m orchestrator.fetch_exchange_rate >> ops.log 2>&1
```

Windows without DSH: `schtasks /Create /TN council-nightly /SC DAILY /ST 04:30 /TR "python -m orchestrator.judge_drift" /SD …` (one task per row above; DSH users get all four from the task panel).

## 2. Data files — who writes what

| File | Writer | Reader | Commit? | Retention |
|---|---|---|---|---|
| `capabilities.json` | bench ingest / self-evolve apply / `sanitize_snapshot.py` (release) | selector, every run | **yes — sanitized snapshot only** (regenerate via script, never hand-edit) | n/a (source) |
| `council-params.json` | you (all knobs; `params.py --show` to inspect) | everything | yes (your tuning) | n/a (source) |
| `balance-snapshot.json` | `query_balance.py` (60s cache) | selector quota factor, console | no (account data) | 24h cache TTL (in-file) |
| `judge-drift.json`, `judge-progress.json` | `judge_drift.py` | console card, metrics | no | judge-progress: weekly rotate |
| `cost-drift.json`, `cost-reconcile-events.jsonl` | `cost_calibrate.py` | console, reconcile | no | drift: 30 days; events: 90 days |
| `evals/pending-runtime-diff.json` | every council run | `update_capabilities --apply` | no | overwritten each `--pending` |
| `evals/runtime-feedback.jsonl` | every council run | nightly apply | no | **90 days (default); older rows archived to `evals/archive/YYYY-MM.jsonl`** (§11) |
| `runs/<ts>/` | every council run | reports, audits, replays | no | keep latest 30 runs; older = `tar.gz` to `runs/archive/` |
| `benchmark/scores/`, `responses/`, `results/` | bench runs | ingest | no | per-run subdirs (rotate yearly) |
| `auto-evolve-state.json` | `auto_evolve.py` (fuse state) | pre-run check | no | 7-day fuse window |
| `exchange-rates*.json*` | fx fetch | cost accounting | no | daily fetch + last-good fallback |

Rule of thumb: **if a script writes it on a schedule, it is not source.**
Source = code + `config/*.example.json` + sanitized snapshot. Everything else
is exhaust.

## 3. Self-evolve apply gate

```bash
python -m orchestrator.update_capabilities --check    # health only
python -m orchestrator.update_capabilities --pending  # build diff, print it
python -m orchestrator.update_capabilities --apply    # apply iff all gates pass
```

Rejections and what they mean:

| Message | Meaning | Fix |
|---|---|---|
| `no_pending` | nothing to apply | normal, go back to sleep |
| `baseHash` mismatch | archive changed since diff was built | re-run `--pending`, review, apply |
| `paused` / drift gate | drift over `pauseWriteDrift` | investigate drift first (§4), do not force |
| judge-gated skip | judge baseline missing/stale | rebuild baseline (§4) |

Never `--apply` blind in a new setup: run `--pending` once, read the diff,
then decide. After that, nightly `--apply` is safe *because* the gates exist.

## 4. Judge baseline management

The judge scores **fixed answers** — score changes can only mean judge drift.
That only holds while prompt/rubric/answers are frozen:

- Change prompt, rubric or any fixed answer → baseline auto-invalidates via
  `promptHash`; the next run rebuilds it (drift history resets — expected).
- Change the judge model (`judgeModel`/`judgeThinking` in params) → run
  `python -m orchestrator.judge_drift --init-baseline` once, manually.
- `--dry` answers "what would run" (item count, judges) for free.

Output exit codes: `0` clean, `2` drift alert (`|drift| > alertThreshold`,
or dim/cross-judge alert), `1` execution failure. `quotaExhausted:true` in
the JSON means quota stopped the run — not a quality signal, do not read it
as one.

## 5. Model lifecycle

New model, four steps (the only supported path — do not hand-edit scores):

1. Add provider/model to your runtime (DSH: `settings.yaml`, hot-reload).
2. Add it to the pool and run bench (`first-bench.sh` or console button).
3. `capability_ingest.py --diff` → review → `--apply`.
4. Next council auto-picks it iff its scores earn it.

Remove: `python orchestrator/retire_candidate.py --list` then
`--model <baseModel> --reason <why>`. Re-identity: `anchor_candidate.py`
(same flags; see `--help`). Both keep backups, bump revision and re-sync the
pending-diff baseHash so apply keeps working.

## 6. Balance & FX

```bash
python -m orchestrator.query_balance --force   # DeepSeek ¥ + MiniMax 5h/week %
python -m orchestrator.fx_status               # 0 ok/yellow, 2 halt
```

- MiniMax 5h at 0% → judge preflight aborts before spending anything;
  mid-run exhaustion trips the fuse after 3 consecutive quota errors.
  **Quota errors never retry — by design.** Wait for the window or top up.
- 429/5xx *do* retry with backoff (30s/60s/120s). A run that takes long but
  progresses is healthy; a run that burns retries on quota is a bug — report it.
- FX: three-level fallback, daily 09:30. Yellow = stale ≥1 trading day;
  halt (`level 2`) pauses CNY accounting, never judging.

## 7. Troubleshooting

| Symptom | Likely cause | Command / fix |
|---|---|---|
| `QUOTA_EXHAUSTED`, run skipped in seconds | 5h window empty (or keys broke) | `query_balance --force`; wait/top-up; check key validity |
| Run slow, `retry_count` high, still progressing | transient 429/5xx | normal; check `elapsed_s` vs backoff ladder |
| `stalled_no_progress` on many items | keys/network/model outage | probe one call; check provider status |
| `score_unparsable` occasionally | model returned non-JSON once | normal noise (means use ok-only); frequent = prompt issue |
| apply: `baseHash` | archive moved under the diff | `--pending` again, review, apply |
| apply: `paused`/drift | real drift | §4, do not force |
| `fx_status` exit 2 | rates stale past halt days | run fetch manually; check network to CFETS/fallback |
| `auto-evolve-state.json` `paused:true` | fuse: 3 straight gate rejections (or 7-day drift) | fix the gate cause, clear `paused` only after |
| `balance_query_failed` / stale snapshot | key wrong or provider API down | same as quota row; snapshot is fail-open |

## 8. Cost cheat sheet

| What | Cost | Where it lands |
|---|---|---|
| council `fast` / `standard` / `deep` | ~¥0.03 / ¥0.15 / ¥0.35 per run | `runs/<ts>/cost.jsonl` (est + plan + actual) |
| first benchmark | ~$15–25, 1–2h | `benchmark/scores/` → ingest |
| nightly judge (36+36+10 calls) | ~80 judge calls, mostly cheap-tier | model usage; compare balance snapshots |
| gold `--fill-one` | 1 exam authored + verified | `benchmark/golden/` |

Philosophy (ADR-001): money picks *among* equal-capability models; it never
caps usage. Runaway protection = max-rounds + wall-clock + empty balance.

## 9. DSH-only vs public

Honest boundary. Everything above runs from this repo **except**:

| DSH-only | Public equivalent |
|---|---|
| 6-tab console (balances, heatmaps, drift curves) | JSON files + `sla_stats.py` + your own plotting |
| task-panel scheduling + 30s tick + timeouts | cron / schtasks (§1) |
| `/api/council/*` incl. `judge-progress` polling + `/metrics` | read the JSON files directly |
| `run_council` / `council_status` / `council_daily_job` tools | the CLI commands in this manual |
| weekly gold-anchor push to maintainer | manual: re-read 5 random gold items monthly |
| model-pool UI buttons | bench/ingest/retire scripts |

## 10. Release checklist (maintainer)

1. `python scripts/sanitize_snapshot.py --council-dir <live> --repo .`
2. Inspect `git diff capabilities.json benchmark/golden/golden-set.json`.
3. `python -m pytest orchestrator tests benchmark/test_ingest.py benchmark/test_regression_gate.py benchmark/test_golden_guard.py -q`
4. CHANGELOG entry + bump `orchestrator/__init__.py` + `pyproject.toml`.
5. Commit; push after data review (scores are public once pushed).

## 11. Runtime feedback retention

`evals/runtime-feedback.jsonl` is appended by every council run (24–50 rows
per day) and full-loaded by `update_capabilities.py` every night. Without
rotation it grows by 8–18K rows per year — three years from now the nightly
load reads tens of MB of ancient history the model will never use. This
section is the policy.

**Defaults** (overridable via CLI flag):

- `retention_days = 90` — rows older than this are archived
- `archive_dir = evals/archive/` — per-month files `YYYY-MM.jsonl`
- `feedback = evals/runtime-feedback.jsonl` — the file every council run writes

**Tool**: `scripts/archive-feedback.py`

```bash
# Dry-run (default): print plan, do not touch anything
python scripts/archive-feedback.py

# Real archive: write new source file + append per-month files
python scripts/archive-feedback.py --apply

# Override retention window
python scripts/archive-feedback.py --retention-days 60 --apply

# Test with a fixed "now"
python scripts/archive-feedback.py --now 2026-09-05T00:00:00Z --apply
```

**Reconciliation guarantees** (this is the part you actually care about):

- **Pre-write**: `total == kept + expired` is enforced by the algorithm (every
  row goes to exactly one bucket: keep, expire-by-month, or conservative-keep
  for unparseable / no-`ts` rows).
- **Post-write per archive file**: each `YYYY-MM.jsonl` is checked:
  `post_count == pre_count + len(items)`. Mismatch → `ArchiveError`, source
  file is **not** replaced (atomic write via `tmp+rename` is gated behind this
  check).
- **Post-write source file**: the new source file is line-counted before
  the atomic replace; mismatch → `ArchiveError`, source unchanged.
- **Any OSError** during the atomic replace → `ArchiveError`, tmp file is
  cleaned up, source file is unchanged.
- **Conservative defaults**: rows without `ts` or with an unparseable `ts`
  are **kept**, never archived. Better to keep noise than to lose signal.

**Where to put it in the daily chain** (§1):

The most useful slot is **before** `update_capabilities --apply` at 04:30,
so the nightly fusion reads a thinner source. Recommended cron snippet
(Linux; for Windows use `schtasks` per §1):

```cron
25 4 * * * cd /path/to/model-council && python scripts/archive-feedback.py --apply >> ops.log 2>&1
```

(The `--apply` is intentional — silent skip on no-op, exit code 2 on
reconciliation failure which the cron logs will catch.)

**Audit / replay**: archived rows stay valid JSONL, one row per line, same
schema as the live file. To re-load a window of history:

```bash
cat evals/archive/2026-06.jsonl evals/archive/2026-07.jsonl evals/archive/2026-08.jsonl \
  | python -c "import json,sys; print(sum(1 for l in sys.stdin if l.strip()))"
# → 1247 rows from those three months
```

**What this does NOT do**:

- Does not touch `evals/pending-runtime-diff.json` (overwritten each
  `--pending`, lives one cycle).
- Does not delete archived rows (intentional — append-only, audit-friendly;
  if you want hard deletion, run `find evals/archive -name '*.jsonl' -mtime
  +365 -delete` on your own schedule, gated by your disk budget).
- Does not change `update_capabilities.py` — it still full-loads the
  (now-trimmed) source file. The savings come from the trim, not from any
  change to the reader.

---

# 中文版运维手册

> v15.9。本文是 README 里原来的 `docs/configuration.md` 断链的落点：
> 配置问题 → 先查[数据文件](#2数据文件谁写谁读)，运维问题 → 查[日常链](#1日常运维链)，报错 → 查[排障](#7排障)。

Council 不是跑完就完的脚本，是要养的系统。单次开会占价值的 10%，剩下 90%
是每天让能力档案保鲜、成本诚实、judge 不漂、考题不过期的循环。本手册讲全
套循环：什么时候跑什么、每块花多少钱、跳过会坏什么、常见报错怎么修。

## 0. 系统地图

| # | 子系统 | 不管它会怎样 | 本地（DSH） | 公开版 |
|---|---|---|---|---|
| 1 | 开会本体（`council_v14.py`） | ——（入口） | 工具/控制台/命令行 | `python -m orchestrator.council_v14 --task …` |
| 2 | 跑分+摄入（`bench/runner.py`、`capability_ingest.py`） | selector 盲选 | 控制台按钮/命令行 | `./scripts/first-bench.sh`、`--diff`/`--apply` |
| 3 | 日常运维链（4 个任务） | 档案腐烂、漂移撒谎 | 任务面板自动 | cron + 下表命令 |
| 4 | 余额/额度（`query_balance.py`） | 额度烧穿才发现 | 控制台 + 60 秒快照 | 同脚本，需配 key（§6） |
| 5 | 汇率（`fetch_exchange_rate.py`、`fx_status.py`） | 人民币记账停摆 | 09:30 自动 | 同脚本（§6） |
| 6 | 自进化落盘（`update_capabilities.py`） | 反馈越积越深、永不落地 | 自动 + 控制台 | `--check/--pending/--apply`（§3） |
| 7 | Judge 漂移+基线（`judge_drift.py`） | judge 静默漂移 | 每晚自动 | 同脚本（§4） |
| 8 | 金标进化（`golden_evolve.py --fill-one`） | 金标池停滞 | 每晚自动 | 同脚本 |
| 9 | 金标人工锚 | 坏题毒化漂移信号 | 每周推给维护者（仅 DSH） | 手动：每月重读 5 道随机题 |
| 10 | 调参（`params.py --show`） | 魔法数字 | 控制台 | `python -m orchestrator.params --show` |
| 11 | 成本对账（`cost_calibrate.py --check`） | 估算与实际越差越远 | 04:00 自动 | 同脚本 |
| 12 | 可观测（SLA、护栏、`/metrics`） | 盲飞 | 控制台 + `/metrics` | `sla_stats.py` + 事件 JSONL |
| 13 | 模型退役/锚定（`retire_candidate.py`、`anchor_candidate.py`） | 死模型赖着不走 | 控制台 + 脚本 | 脚本（`--help`） |
| 14 | 发版（`sanitize_snapshot.py`、CHANGELOG） | 公开仓腐烂 | 维护者手动 | §10 |

## 1. 日常运维链

4 个任务，按顺序来。时间是维护者的（北京时间），你定自己的时间，但顺序
和间隔要保（后一个读前一个的输出）。

| 时间 | 命令 | 上限 | 退出码 | 跳过后果 |
|---|---|---|---|---|
| 02:00 | `python -m benchmark.auto_evolve` | 7200 秒 | 0 正常；非 0 失败——先看 `auto-evolve-state.json`：`paused:true` 是熔断跳了，**不许**强跑 | 考题与现实脱节，过期题继续烧钱 |
| 04:00 | `python -m orchestrator.cost_calibrate --check` | 120 秒 | 0 达标；**2 = 漂移超阈（告警，不算失败）** | 估算静默偏离实际 |
| 04:30 | `python -m orchestrator.judge_drift` → `python -m orchestrator.update_capabilities --apply` → `python -m benchmark.golden_evolve --fill-one` | 1500 秒/120 秒/600 秒 | judge：0 正常、2 漂移告警、1 执行失败；输出里有 `QUOTA_EXHAUSTED` = 全跳过、不重试 | judge 漂移和待落盘反馈永不落地；金标停滞 |
| 09:30 | `python -m orchestrator.fetch_exchange_rate` | 120 秒 | 0 正常（三级 fallback：CFETS→备用→最近成功） | 汇率过期；人民币记账在超过 `staleHaltDays` 个交易日后停摆（见 `fx_status.py`） |

cron 示例（Linux；日志就是审计）：

```cron
0 2 * * * cd /path/to/model-council && python -m benchmark.auto_evolve >> ops.log 2>&1
0 4 * * * cd /path/to/model-council && python -m orchestrator.cost_calibrate --check >> ops.log 2>&1
30 4 * * * cd /path/to/model-council && python -m orchestrator.judge_drift >> ops.log 2>&1 && python -m orchestrator.update_capabilities --apply >> ops.log 2>&1 && python -m benchmark.golden_evolve --fill-one >> ops.log 2>&1
30 9 * * * cd /path/to/model-council && python -m orchestrator.fetch_exchange_rate >> ops.log 2>&1
```

没 DSH 的 Windows：`schtasks /Create` 每行建一个（DSH 用户任务面板自带 4 个）。

## 2. 数据文件：谁写谁读

| 文件 | 写 | 读 | 提交？ | 保留期 |
|---|---|---|---|---|
| `capabilities.json` | 跑分摄入/自进化落盘/发版脱敏脚本 | 每次开会的 selector | **提交——只许脱敏快照**（用脚本生成，永不手改） | n/a（源码） |
| `council-params.json` | 你（全部开关；`params.py --show` 查看） | 所有模块 | 提交（你的调参） | n/a（源码） |
| `balance-snapshot.json` | `query_balance.py`（60 秒缓存） | selector 额度因子、控制台 | 不提交（账户数据） | 文件内 24h 缓存 TTL |
| `judge-drift.json`、`judge-progress.json` | `judge_drift.py` | 控制台卡片、metrics | 不提交 | judge-progress 周轮换 |
| `cost-drift.json`、`cost-reconcile-events.jsonl` | `cost_calibrate.py` | 控制台、对账 | 不提交 | drift 30 天；events 90 天 |
| `evals/pending-runtime-diff.json` | 每次开会 | `update_capabilities --apply` | 不提交 | 每次 `--pending` 覆盖 |
| `evals/runtime-feedback.jsonl` | 每次开会 | 每晚落盘 | 不提交 | **90 天（默认）；更早按月归档到 `evals/archive/YYYY-MM.jsonl`**（§11） |
| `runs/<时间>/` | 每次开会 | 报告、审计、复盘 | 不提交 | 留最近 30 次；更早 `tar.gz` 到 `runs/archive/` |
| `benchmark/scores/`、`responses/`、`results/` | 跑分 | 摄入 | 不提交 | 每跑次子目录（年度轮换） |
| `auto-evolve-state.json` | `auto_evolve.py`（熔断状态） | 跑前预检 | 不提交 | 7 天熔断窗 |
| `exchange-rates*.json*` | 汇率抓取 | 成本核算 | 不提交 | 每日抓取 + 最近一次成功值兜底 |

一句话：**定时任务写出来的东西都不是源码。** 源码 = 代码 + `config/*.example.json` +
脱敏快照，其余都是排气。

## 3. 自进化落盘门禁

```bash
python -m orchestrator.update_capabilities --check    # 只体检
python -m orchestrator.update_capabilities --pending  # 生成 diff 并打印
python -m orchestrator.update_capabilities --apply    # 门禁全过才落盘
```

拒绝信息对照：

| 提示 | 意思 | 修法 |
|---|---|---|
| `no_pending` | 没东西可落 | 正常，回去睡觉 |
| `baseHash` 对不上 | 生成 diff 后档案被改过 | 重跑 `--pending`，看完再 apply |
| `paused`/漂移门禁 | 漂移超 `pauseWriteDrift` | 先查漂移（§4），不许强行落 |
| judge 门禁跳过 | 基线缺失/过期 | 重建基线（§4） |

新环境第一次：先 `--pending` 看一遍 diff，想清楚再 `--apply`。之后每晚自动
`--apply` 是安全的——**正因为有这几道门**。

## 4. Judge 基线管理

Judge 打的是**固定答案**——答案不变，分数变了只能是 judge 漂了。但这要求
prompt/rubric/答案冻结：

- 改 prompt、rubric 或任一固定答案 → `promptHash` 自动失效，下次跑重建基线
  （漂移史清零——预期行为，不是 bug）。
- 换 judge 模型（params 里 `judgeModel`/`judgeThinking`）→ 手动跑一次
  `python -m orchestrator.judge_drift --init-baseline`。
- `--dry` 免费回答"跑起来是什么规模"（几道题、哪两个 judge）。

退出码：`0` 干净、`2` 漂移告警（总漂移/分维度/交叉 judge 任一超阈）、`1` 执行
失败。JSON 里 `quotaExhausted:true` 是额度中断——不是质量信号，别当漂移读。

## 5. 模型生命周期

新模型四步（唯一正道——**不许手改分数**）：

1. 运行环境加 provider/model（DSH：`settings.yaml`，热加载）。
2. 进池 + 跑分（`first-bench.sh` 或控制台按钮）。
3. `capability_ingest.py --diff` 看 → `--apply` 落。
4. 下次开会 selector 自动按分选用，分不够就选不上。

退役：`python orchestrator/retire_candidate.py --list` 先看，再
`--model <baseModel> --reason <原因>`。改身份：`anchor_candidate.py`
（参数同理，`--help` 为准）。两个脚本都留备份、升 revision、同步
pending-diff 的 baseHash，落盘链不断。

## 6. 余额与汇率

```bash
python -m orchestrator.query_balance --force   # DeepSeek 余额 + MiniMax 5h/周
python -m orchestrator.fx_status               # 0 正常/黄灯，2 停机
```

- MiniMax 5h 到 0% → judge 预检在第一个调用前直接走人；跑中途见底连吃 3 个
  额度错整轮熔断。**额度错永不重试——设计如此。** 等窗口或充值。
- 429/5xx 会退避重试（30/60/120 秒）。跑得慢但有进展 = 健康；额度错还烧
  重试 = bug，报给我（报给维护者）。
- 汇率三级 fallback，每天 09:30。黄灯 = 过期≥1 个交易日；`level 2` 停机只停
  人民币记账，不停判分。

## 7. 排障

| 症状 | 大概率 | 修法 |
|---|---|---|
| `QUOTA_EXHAUSTED`，几秒就跳过 | 5h 窗口空了（或 key 坏了） | `query_balance --force`；等/充值；验 key 有效性 |
| 跑得慢，`retry_count` 高，但在走 | 瞬态 429/5xx | 正常；对 `elapsed_s` 和退避阶梯 |
| 大面积 `stalled_no_progress` | key/网络/模型端挂了 | 单发 probe；看服务商状态页 |
| 偶发 `score_unparsable` | 模型某次没回 JSON | 正常噪声（均值只算 ok 的）；频发 = prompt 问题 |
| 落盘：`baseHash` | diff 生成后档案动过 | 重跑 `--pending`，看完再落 |
| 落盘：`paused`/漂移 | 真漂移 | §4，不许强落 |
| `fx_status` 退出 2 | 汇率过期超停机天数 | 手动抓一次；查到 CFETS/备源的网络 |
| `auto-evolve-state.json` 里 `paused:true` | 熔断：门禁连挂 3 次（或漂移超 7 天） | 修门禁原因，修好再清 `paused` |
| `balance_query_failed`/快照过期 | key 错或服务商 API 挂 | 同额度行；快照 fail-open，不杀主流程 |

## 8. 成本速查

| 项目 | 花费 | 落哪 |
|---|---|---|
| council `fast`/`standard`/`deep` | 约 ¥0.03/¥0.15/¥0.35 每次 | `runs/<时间>/cost.jsonl`（估算+预算+实际三口径） |
| 首次全量跑分 | 约 $15–25，1–2 小时 | `benchmark/scores/` → 摄入 |
| 每晚 judge（36+36+10 次调用） | 约 80 次 judge 调用，多为便宜档 | 对比余额快照前后 |
| 金标 `--fill-one` | 出 1 题 + 验证 | `benchmark/golden/` |

哲学（ADR-001）：钱只在**同等能力**的模型之间选，不做用量上限。防失控靠
最大轮数 + 墙钟 + 余额烧空。

## 9. DSH 专属 vs 公开版

诚实边界。上面所有命令本仓都能跑，**除了**：

| 仅 DSH | 公开版等价 |
|---|---|
| 6-Tab 控制台（余额、热力、漂移曲线） | JSON 文件 + `sla_stats.py` + 自己画图 |
| 任务面板调度 + 30 秒 tick + 超时 | cron / schtasks（§1） |
| `/api/council/*`（含 `judge-progress` 轮询）+ `/metrics` | 直接读 JSON 文件 |
| `run_council` / `council_status` / `council_daily_job` 工具 | 本手册的命令行 |
| 每周金标人工锚推送 | 手动：每月重读 5 道随机金标 |
| 模型池 UI 按钮 | 跑分/摄入/退役脚本 |

## 10. 发版清单（维护者）

1. `python scripts/sanitize_snapshot.py --council-dir <live> --repo .`
2. 看 `git diff capabilities.json benchmark/golden/golden-set.json`。
3. `python -m pytest orchestrator tests benchmark/test_ingest.py benchmark/test_regression_gate.py benchmark/test_golden_guard.py -q`
4. CHANGELOG + `orchestrator/__init__.py` + `pyproject.toml` 升版本。
5. 提交；数据过目后再 push（分数推出去就是公开的）。

## 11. 反馈保留策略

`evals/runtime-feedback.jsonl` 每次开会都追加（每天 24–50 行），
每晚 `update_capabilities.py` 全文加载融合能力档案。不轮换的话一年涨
8–18K 行，三年后每晚多读几十 MB 历史——模型永远用不上。这节讲治理策略。

**默认值**（CLI 可覆盖）：

- `retention_days = 90` — 超过这个天数的行才归档
- `archive_dir = evals/archive/` — 按月归档 `YYYY-MM.jsonl`
- `feedback = evals/runtime-feedback.jsonl` — 每次开会写入的文件

**工具**：`scripts/archive-feedback.py`

```bash
# dry-run（默认）：打印计划，不动文件
python scripts/archive-feedback.py

# 实际归档：写新源文件 + 追加按月归档
python scripts/archive-feedback.py --apply

# 自定义保留窗口
python scripts/archive-feedback.py --retention-days 60 --apply

# 测试用固定“今天”
python scripts/archive-feedback.py --now 2026-09-05T00:00:00Z --apply
```

**对账保证**（这是重点）：

- **写前**：`total == kept + expired` 算法强制——每行只进一个桶（保留 /
  按月过期 / 保守保留——JSON 解析错或无 `ts` 字段的行）。
- **写后按归档文件**：每个 `YYYY-MM.jsonl` 都要查 `post_count ==
  pre_count + len(items)`，不等则 `ArchiveError`，源文件不替换（原子写
  `tmp+rename` 在对账后才走）。
- **写后源文件**：新源文件在原子替换前行数必须对得上，否则 `ArchiveError`，
  源文件不变。
- **任何 OSError**（原子替换阶）→ `ArchiveError`，tmp 清理，源文件不变。
- **保守默认**：无 `ts` 或 `ts` 格式错的行**永远保留**不归档。丢了信号不如
  留噪声。

**上哪纳进日常链**（§1）：

最有用的位置是 **04:30 `update_capabilities --apply` 之前**——这样每晚
融合读的是更瘦的源。Linux cron 示例（Windows 用 `schtasks`，同 §1）：

```cron
25 4 * * * cd /path/to/model-council && python scripts/archive-feedback.py --apply >> ops.log 2>&1
```

（`--apply` 是故意的——无操作时静默跳过；对账失败退出码 2，cron 日志会留下。）

**审计/回放**：归档行仍是合法 JSONL，每行一条，schema 与在用文件一致。
要回看某窗历史：

```bash
cat evals/archive/2026-06.jsonl evals/archive/2026-07.jsonl evals/archive/2026-08.jsonl \
  | python -c "import json,sys; print(sum(1 for l in sys.stdin if l.strip()))"
# → 这三个月共 1247 行
```

**不做什么**：

- 不动 `evals/pending-runtime-diff.json`（每次 `--pending` 覆盖，活一轮）。
- 不删归档行（故意 append-only，便于审计；真要硬删，备份+自己设周期跑
  `find evals/archive -name '*.jsonl' -mtime +365 -delete`，按你磁盘预算）。
- 不改 `update_capabilities.py`——它仍全文加载（已瘦身的）源文件。节省
  来自裁剪，不来自改动读取端。
