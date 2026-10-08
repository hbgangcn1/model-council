# Model Council v15: Current-State Design

Status: as implemented today (2026-10-03). This doc describes the code, not the history. Numbers and names below come from the files named beside them. Anything not confirmed in code is marked TBD.

## 1. Architecture

One Python orchestrator runs everything: `orchestrator/council_v14.py` (`run_council()`). The file named `orchestrator/council.py` is a trap, not the entry point.

A run flows through five stages:

1. Decompose: split the task into 2 to 4 subtasks, each with a weight vector over 9 capability dims. Decompose itself is assigned dynamically (`_pick_role` with `DECOMPOSE_WV`), limited to low thinking levels (`ROLE_ALLOWED_LEVELS = off/low/minimal`), falling back to `deepseek-v4-flash/low`.
2. Assign: per subtask, pick executors and verifiers from the capability archive (see section 2).
3. Converge: run rounds of execute, verify, score `S_r`, decide (see section 3).
4. Synthesize: write the answer from the best round's outputs, not the last round's (`_resolve_synthesis_outputs`, `terminator.best_round_index`). Synth gets no tools.
5. Learn: append runtime feedback, update capability scores, log costs and decisions.

Every run leaves `runs/<timestamp>/` with `task.md`, `subtasks.json`, `decisions.jsonl`, `cost.jsonl`, `rounds.jsonl` (with termination audit), per round outputs, `result.json`, and `report.md`.

CLI: `--task/-t`, `--tier/-p (fast|standard|deep)`, `--mode/-m (report|inline)`, `--dry`, `--facts` (host snapshot file), `--resume` (run dir, true resume from `state.json`), `--format` (repeatable, `html|docx`).

## 2. Selection: selector plus guardrails

`orchestrator/selector.py`, `select()`.

Score per candidate:

```
score = Σ weight[d] × rank_norm[d] − λ × cost_term − μ × latency_term + diversity_bonus
```

Details that matter:

- Rank normalization is on by default (`selection.rankNormalize`). Each dim maps to 1..100 (first place 100, last place 1), so near identical raw scores (9.5 to 10) still separate. The archive keeps raw 0..10 scores.
- Cost term is gentle by design: `λ × min(0.5, log1p(cost × 1000))`. Tiers set λ (fast 0.15, standard 0.1, deep 0.02, in `council-params.json`). Cost nudges ties, it never blocks.
- Latency term uses measured `latencyP50Ms` when present, else a thinking level proxy (`THINKING_LATENCY_PROXY`).
- Extras: Pareto frontier soft bonus (0.1), Elo soft penalty, diversity bonus (0.3), epsilon greedy (0.05). Tie break order: score, cost up, latency up, cid.
- Candidates are keyed `model__thinking` (for example `deepseek-flash__low`). Same base model at different thinking levels counts as different candidates.

Guardrails, in strict priority order (one reason reported per rejection, logged to `guardrail-events.jsonl` with run id, threshold, measured value, caps revision):

1. `identity_unknown`
2. `unstable_member_excluded_from_pool`
3. `thinking_not_allowed`
4. `circuit_open` (3 state breaker with exponential backoff; half open probes are single flight)
5. `balance_exhausted` (quota factor infinity)
6. `self_verify_ban` (an executor can't verify its own output, enforced at vendor group level)

Static rejects (unknown identity, unstable, allowlist, self verify) are prefiltered before scoring, one `pool_excluded` summary event per call. All tunables (thresholds, bonuses, epsilon, mu) live in `council-params.json` under `selection`, `circuit`.

Capacity math uses effective vendors (`effective_vendors()`: who's usable right now by balance and breaker state), not who's listed in the archive. Dead vendors can't eat quota.

## 3. Termination

`orchestrator/terminator.py`, `decide()`. No round cap, no budget cap. Both were deleted on purpose.

- `converged`: `S_r >= θ`, θ = 9.5 in all three tiers. Kept, but rarely hit (historical best ≈ 9.33).
- `early_stop` (the normal ending): the round's `S_r` must beat the prior best by more than ε (`delta = 0.2`); two consecutive rounds without such an improvement (`plateauRounds = 2`) ends the run with the best round's material.
- `stalled`: same hard gate issue two rounds in a row with no improvement.
- `forced`: only the runaway guard (`wallBudget.runawayGuardS = 10800`, 3h). A normal run should never hit it.
- `rework`: otherwise, carry the rework list into the next round (incremental, only listed items rerun).

Best round uses the same ε rule as the plateau check (`best_round_index`), so the stop reason and the chosen material can't disagree.

## 4. Cost model (cost as citizen)

Cost informs selection, it doesn't control execution. There is no budget termination anywhere in the loop, and the precheck (`orchestrator/budget.py`) only reports (`status` is always `"report"`).

```
effectiveCost = base × thinkingMult × quotaFactor
base = ((in × (1−chr) + cache × chr) × inPrice + out × outPrice) / 1e6, calibrated per model and role
```

Books are kept in CNY. FX rate refreshes daily (CFETS middle rate); a stale rate only degrades estimate confidence (`fx_warning`), never blocks a run. Planning and actuals share one shape (`estBase` vs `actual`) so drift is comparable and calibrates over time (`token_profiles`). Tier yuan labels in the skill (¥0.03/0.15/0.35) are planning labels, not enforced caps. TBD: whether those labels still match measured averages.

## 5. Capability files

Three files, three jobs:

- `capabilities.json` (schemaVersion 2, revision grows monotonically): the only selection data source. Entries keyed `model__thinking` with 9 dim scores, sample counts, runtime stats, cost and latency profile, stability flags, `vendorGroup`, `_source_run_ids` for traceability. Updated two ways: benchmark ingest (reviewed diff, see section 7) and runtime feedback fusion after each run (`orchestrator/update_capabilities.py`, file lock, pre write validation, `|Δ| < 0.5` not written to cut noise).
- `model-pool.json` (schemaVersion 1): membership list at model granularity. Adds and removals are fully manual (Robert's call); auto detection only proposes and alerts. Members without benchmark scores get no archive entry, so they can't be selected. Missing file falls back to bridge models as active (migration path only).
- `model-tier-bridge.json` via `bridge.py`: the single source for thinking levels and wire params. `orchestrator/tier_bridge.py` delegates to it and fails loud on unknown models or levels (no silent 8192 token fallback). Council never keeps its own level table.

## 6. Judge auto selection

No hardcoded judge names in code. Resolution order: explicit params value, then `judge_select` ranking, then last known good file.

- `orchestrator/judge_select.py`: `select_judge()` ranks pool members by `judge-profiles.json` scores (or capability mean as fallback), excludes banned base models, sinks exhausted providers without dropping them; `last_known_good()` reads `judge-drift.json` / `judge-baseline.json`; `resolve_auto()` loads pool, archive, and balance snapshot from disk.
- `benchmark/judge_qualify.py`: the exam that produces `judge-profiles.json`. Pool models at low thinking levels score golden good and bad answers against rubrics; metrics are calibration error (lower better), discrimination (higher better), stability vs last round.
- `orchestrator/judge_drift.py`: daily golden set self scoring against baseline. Drift beyond `alertThreshold` logs to `judge-drift-events.jsonl` and exits 2. Prompt or rubric changes force baseline rebuild (tracked by hash).

## 7. Benchmark ingest with judge aperture down weight

`benchmark/capability_ingest.py`: per question scores merge into the archive by dim EMA. Default flow writes a pending diff for human approval; `--apply` lands it.

Judge aperture rule: subjective scores from a same vendor judge get down weighted (`SAME_VENDOR_WEIGHT = 0.5`). The down weighted rows are listed in the diff output for review. This answers the old self scoring problem (a judge grading its own vendor's models).

## 8. Tool loop (5 tools)

`orchestrator/tool_loop.py`, driven through the DSH bridge (`tool-exec`), read leaning, all results wrapped as untrusted external data with per item truncation (6000 chars).

| Tool | What it does |
|---|---|
| `web_search` | Search the web, 1 to 4 queries per call |
| `web_fetch` | Read one search result URL in full |
| `read_file` | Read a local file (allowlisted dirs, 200KB cap) |
| `list_dir` | List a directory (max depth 6) |
| `search_content` | Grep style search in allowlisted dirs |

Budgets (all in `council-params.json`, `tools`): exec pool 60, verifier pool 60, run backstop 500, max 3 tool rounds each for exec and verifier, 5 search results max. Exhaustion degrades to finishing with available material, never kills the round. Synth has no tools: it may only cite sources already in the material.

## 9. Single vendor single pass ladder

`_gate_mode()` in `council_v14.py` decides at startup from effective vendors:

- 0 vendors: refuse (`insufficient_models`, exit 0, no spend, hint included).
- 1 vendor: `single_pass`. Executors only, `verifiers = []`, report header and `result.json` carry `UNVERIFIED_SINGLE_VENDOR`, stated confidence capped at 0.5.
- 2 or more: full council with cross vendor verification.

Cross vendor means vendor groups (`vendorGroup`), not base model names: two models from one vendor can't verify each other. When verifiers drop below `minValidVerifiers = 2`, the runner tries a different vendor substitute first, else logs `verifier_underverified` into warnings instead of silently scoring unreviewed.

## 10. Multi format outputs

`--format html` / `--format docx` (repeatable, off by default). After the run, `orchestrator/render_formats.py` converts the finished `report.md` (canonical, no reinference) into `report.html` (self contained) and/or `report.docx` next to it, stdlib only. Failures log `formats_failed` and don't touch the markdown.

## 11. File inventory

Root of `~/.dsh/council/`, one line each:

- `orchestrator/council_v14.py`: the orchestrator, all five stages plus CLI.
- `orchestrator/selector.py`: scoring, guardrails, breaker, cost function.
- `orchestrator/terminator.py`: plateau based stop rules.
- `orchestrator/tool_loop.py`: 5 tool multi round calling with split budgets.
- `orchestrator/budget.py`: report only balance precheck.
- `orchestrator/judge_select.py` / `orchestrator/judge_drift.py`: judge ranking and drift watch.
- `orchestrator/verify_claims.py`: factual claim check pipeline behind verifier prompts.
- `orchestrator/update_capabilities.py`: runtime feedback fusion into the archive.
- `orchestrator/render_formats.py`: report.md to html/docx.
- `orchestrator/params.py` + `council-params.json`: all tunables, file wins.
- `orchestrator/tier_bridge.py` + `bridge.py` + `model-tier-bridge.json`: level and wire source of truth.
- `orchestrator/stream_llm.py`, `llm_client.py`, `llm_transports/`: model calling and DSH bridge transport.
- `orchestrator/token_profiles.py`, `cost_context.py`, `cost_calibrate.py`: usage EWMA, cost typing, estimate calibration.
- `orchestrator/pairwise.py` + `elo.json`: cross model Elo from end of run comparisons. (TBD: end of run call site assumed from v15.2, not rechecked.)
- `orchestrator/calibration.py`, `caps_guard.py`, `config_loader.py`, `file_lock.py`, `query_balance.py`, `fetch_exchange_rate.py`, `fx_status.py`, `sla_stats.py`, `dry_run.py`, `anchor_candidate.py`, `retire_candidate.py`, `failed_runs_report.py`: calibration, archive guards, config, locking, balance, FX, stats, dry run, pool anchors, retirement proposals, failure reports.
- `pool.py` + `model-pool.json`: manual membership list.
- `capabilities.json`: the scored archive (selection source).
- `pricing-profiles.json`, `token-profiles.json`, `thinking-profiles.json`, `cost-tiers.json`, `presets.json`: pricing, usage, thinking, tier, and tier preset tables.
- `benchmark/capability_ingest.py`: score ingest with aperture down weight.
- `benchmark/judge_qualify.py` + `judge-profiles.json` (council 根目录，首次考核运行后生成): judge exams and results.
- `benchmark/` (runner, golden set, cases): benchmarks, baselines, applied diffs.
- `json_repair.py` (council 根目录): lenient JSON parsing (fences, truncation, escapes).
- `audit_council_orchestrator.py`, `audit_bridge_vs_caps.py`: pre change audit scripts, run before touching council or pool.
- `runs/`, `evals/runtime-feedback.jsonl`, `guardrail-events.jsonl`, `judge-drift.json`, `circuit-state.json`, `balance-snapshot.json`: run outputs, feedback, guard log, drift state, breaker state, balance cache.
- `DESIGN-v14.md`: previous design doc, superseded by this file. `WORKFLOW.md`, `tier-alignment-plan.md`, `v15.5-roadmap.md`: process and planning notes, non normative.

## Appendix: changelog (v14 to v15 in 20 lines)

1. Cost demoted from termination to selection signal; budget caps and FX halt removed.
2. Wall clock demoted to 3h runaway guard; plateau (2 rounds, ε 0.2) is the stop rule.
3. θ unified at 9.5 across tiers; round caps deleted; synth uses best round.
4. Rank normalized scoring (1..100) beats raw score clustering; Pareto/Elo/diversity kept.
5. Guardrails fixed at 6 with priority order; availability snapshot gates startup.
6. Vendor groups replace base model counting for hetero and verifier rules.
7. Zero vendor refuses, one vendor runs single pass unverified, two run full.
8. Tool loop grew from 2 network tools to 5 (plus 3 local read tools) with split pools.
9. Judges auto selected by exam score; drift watched daily; same vendor scores halved.
10. Pool membership fully manual; tier bridge is the single level source, fails loud.
11. Report synthesis hides internals (no s ids, S r, model names); formats html/docx added.
12. Resume is real (state.json checkpoint); decompose falls back to one subtask plan.
13. Freshness rules and claim verification hardened; self labeled estimates exempted.
14. Best round rule unified with ε; min valid verifiers 2 with substitute then warn.
15. (Lines 15 to 20 reserved: only 14 material changes; no filler added.)
