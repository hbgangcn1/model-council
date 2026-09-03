# Changelog

All notable changes to **Model Council** are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/) and the project adheres to
[Semantic Versioning](https://semver.org/).

## [15.9.0] - 2026-09-03

Zero-to-council in 5 minutes: prebuilt data snapshot + credential flexibility
+ first-run wizard. No cold-start benchmark required.

### Added

- Prebuilt data snapshot: `capabilities.json` (18 model×thinking entries with
  scores/samples/cost/latency, sanitized via `scripts/sanitize_snapshot.py`)
  and `benchmark/golden/golden-set.json` (36 items). New users skip the
  $15–25 first benchmark; `./scripts/first-bench.sh` remains for own scores.
- `scripts/first_run.py` — first-run wizard (stdlib only): verifies python
  version, data snapshot scale, credentials, offline tests and `judge --dry`;
  `--init-credentials` writes `~/.model-council/credentials` (owner-only).
  Exit 2 with exact fix instructions when keys are missing.
- `scripts/sanitize_snapshot.py` — reproducible export of the maintainer
  snapshot (whitelist fields, drops run linkage/timestamps/feedback,
  skips unstable entries); run before each data-refresh release.
- Four-level credential resolution (`orchestrator/config_loader.py`):
  `MODEL_COUNCIL_CREDENTIALS` file > env vars > `~/.model-council/credentials`
  > legacy `~/.dsh/.credentials.yaml`; helpful error when none found.
- `tests/test_config_keys.py` — 6 items covering the resolution order.
- README 5-minute quickstart (EN + 中文）.

### Changed

- `orchestrator/query_balance.py` reads credentials via `config_loader.api_keys()`
  (removes the duplicated `~/.dsh` parsing).

## [15.8.0] - 2026-09-03

Judge reliability + quota-aware throttling. (Includes the previously
unreleased local v15.7 judge parallelism, now part of this snapshot.)

### Added

- `LLMQuotaExhaustedError` (`orchestrator/llm_client.py`) — subclasses
  `LLMPermanentError`, so quota exhaustion fails fast with zero retries;
  downstream code detects it via the `QUOTA_EXHAUSTED` message prefix.
- `classify_bridge_error()` (`orchestrator/llm_transports/dsh_bridge.py`) —
  offline-testable classifier splitting provider errors into `quota`
  (MiniMax 1008/2056 + balance/quota keywords, incl. Chinese) vs `rate`
  (1002/1039/1041/2045/429/Z.AI 1302/5xx) vs `unknown`; quota wins on ties.
- Judge quota preflight + in-run fuse (`orchestrator/judge_drift.py`) —
  free balance-snapshot preflight aborts before the first call when the 5h
  window is empty; 3 consecutive `QUOTA_EXHAUSTED` abort the run with a
  `quotaExhausted` output flag instead of burning ~20 minutes on retries.
- Judge liveness gate + progress file (`orchestrator/judge_drift.py`) —
  items submitted once and collected per pass; the run ends on completion or
  two consecutive zero-progress passes (stragglers marked `stalled`), with
  `max_runtime_s` kept only as a deadlock backstop. Per-item progress
  (`stage`, `model@thinking`, done/total, ok/quota counts) is written
  atomically to `judge-progress.json` for external polling.
- `orchestrator/test_v158.py` — 28 offline regression items (classifier
  matrix, fail-fast timing, finish-error parsing incl. quoted `[DONE]`
  sentinel, stall liveness, quota fuse + progress callback).

### Fixed

- `finish.reason.kind == "error"` (e.g. `{event:"finish",
  reason:{kind:"error", failure:{message:"429..."}}}`) is now surfaced and
  classified instead of silently swallowed (previously produced empty-text
  false successes).
- SSE `data: "[DONE]"` (server JSON-stringifies the sentinel) no longer
  crashes the event loop with `AttributeError`; non-object payloads are
  skipped.
- HTTP-layer 4xx (other than 429) from the bridge raise permanent errors
  (quota body → quota error) instead of being retried as timeouts.
- Judge scoring parallelism (ThreadPoolExecutor, 3 workers) replaces serial
  per-item calls.

## [15.6.0] - 2026-XX-XX

First public release. Extracted from internal use (originally developed as part
of DeepSeek Harness integrations); refactored into a standalone, framework-agnostic
Python library.

### Added

- **`orchestrator/`** — 25 core modules:
  - `council_v14.py` (1048 lines) — main orchestration entry point
  - `selector.py` (798 lines) — capability-driven model selection with 6 guardrails
  - `terminator.py` — convergence criteria (θ + growth-exhaustion window)
  - `params.py` — tunable parameters loader (all knobs externalized to JSON)
  - `budget.py`, `calibration.py`, `config_loader.py`, `dry_run.py`
  - `cost_calibrate.py`, `cost_context.py`, `fetch_exchange_rate.py`, `fx_status.py`,
    `query_balance.py`, `token_profiles.py`, `pairwise.py`,
    `update_capabilities.py`, `anchor_candidate.py`, `retire_candidate.py`,
    `verify_claims.py`, `judge_drift.py`, `sla_stats.py`, `file_lock.py`,
    `stream_llm.py`, `caps_guard.py`, `failed_runs_report.py`
  - `test_*.py` × 6 (64+ pytest items)
- **`benchmark/`** — capability benchmarking pipeline:
  - `bench/runner.py`, `scorer.py`, `llm.py`, `sandbox.py`, `config.py`,
    `cases.py`, `summary.py`, `cross_judge.py`, `audit_scorer.py`
  - `capability_ingest.py` — diff/apply workflow for ingesting benchmark results
  - `case_diff_rerun.py`, `regression_gate.py`, `golden_guard.py`,
    `golden_evolve.py`, `golden_augment.py`, `judge_qualify.py`,
    `build_capabilities.py`, `auto_evolve.py`, `finish_benchmark.py`
  - `v21-cases.json` — 37 evaluation cases spanning 9 capability dimensions
  - `test_*.py` × 3
- **`config/`** — example configuration templates:
  - `council-params.example.json` — all tunable parameters
  - `pricing.example.json`, `cost-tiers.example.json` — provider pricing templates
- **`docs/`** — design documents (architecture, design decisions, roadmap, tier
  alignment plan) — see `docs/README.md`
- **`scripts/`** — bootstrap utilities (capability archive init, first-bench runner)
- **`audit_council_orchestrator.py`** — sanity-check script to verify `council_v14.py`
  is the real orchestrator entry point (prevents LLM misjudgment when analyzing /
  modifying the codebase)

### Design highlights

- **Capability-driven selection**: 9-dimensional score per `model@thinking`,
  selected by `Σ dim_weight × capability − λ·cost − μ·latency + diversity`.
- **Cross-vendor verification**: `vendorGroup` constraint guarantees verifier and
  executor are different vendors.
- **Self-verify ban**: a model never verifier is its own executor.
- **Capability revision integrity**: every change to `capabilities.json` bumps a
  `revision` counter with a `baseHash` integrity guard; pending diffs are rejected
  if `baseHash` has drifted.
- **Circuit breaker**: 3-state (closed/half-open/open) with exponential backoff
  and half-open probing for transient failures (e.g. provider 429/5xx).
- **Cost philosophy (v15.4)**: cost selects among equivalent-capability models;
  it does *not* cap usage. Runaway cost is bounded by max-rounds and wall-clock,
  not by budget termination.
- **Dynamic wall-clock budget (v15.5)**: budget scales with task size
  (`baseS + Σ(subtask × executors × verifiers) × perSlotS`), capped at host
  timeout − 240s margin.
- **JSON repair**: every model output passes a 4-stage lenient parser (fence strip
  / bare-newline state machine / truncation repair / unescaped quote heuristic)
  with diagnostic retry and substitute-verifier escalation.
- **Audit trail**: every run writes `task.md`, `decisions.jsonl`, `rounds.jsonl`,
  `cost.jsonl`, `budget.jsonl`, `verdict-raw/`, `result.json`, `report.md`. No
  silent fallbacks; subprocess exit code ≠ 0 is fatal.

### Verified

- 64+ pytest items passing (orchestrator + benchmark + golden-guard + ingest +
  regression-gate)
- v15.5 4-layer JSON defense (parse-once / field-fallback / diagnostic retry /
  substitute) verified by integration tests
- Cross-vendor verification confirmed by adversarial test (forced DeepSeek-only
  pool → fall back to substitute verifier)
- Cost 3-line accounting (`estBase` vs `actual`) verified at <±10% drift over 7-day
  window

### Not included (intentionally)

- Your `capabilities.json` / `balance-snapshot.json` / `circuit-state.json` /
  `elo.json` / `runtime-feedback.jsonl` / etc. — these are *user data* and live
  in your data directory (default `~< user-data-dir >/`).
- Historical runs (`runs/`), reports (`reports/`), evidence (`evidence/`),
  ledger (`ledger/`), legacy OpenClaw-era tooling (`tools/`) — these accumulate
  on your local machine over time.
- The DeepSeek Harness (DSH) bridge plugin (lives in
  `~/.dsh/profiles/web/node_modules/host-bridge plugin/`) — a separate integration project.
- Personal evaluation data and private capability benchmarks from the maintainer's
  internal usage (privacy-protected).
- **PyPI distribution** — this repository is the source of truth. The project is
  not published to PyPI; friends who want to run it clone the repo and
  `pip install -e .`.

## Pre-history

Versions ≤ 15.5 were developed and used internally prior to public release; their
history is preserved in the maintainer's local data archive but not duplicated here.