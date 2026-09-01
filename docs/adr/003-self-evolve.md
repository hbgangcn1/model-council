# ADR-003: Self-Evolve Loop

**Status**: Accepted (v15.0, refined through v15.5)
**Version**: v15.0+

## Context

Model capability drifts over time: a model that scored 9.5 last month may score
8.0 this month due to a silent update, a routing change at the provider, or
the addition of new training data. Manual re-benchmarking is impractical at
the cadence required to keep the capability archive accurate.

We want a system that **automatically detects capability drift** and **updates
the capability archive** — but without silently mutating critical data without
human oversight. The capability archive is the source of truth for model
selection; corrupting it would silently degrade all future runs.

## Decision

We adopt a 4-stage self-evolve loop:

### Stage 1: Runtime feedback (every run)

Every council run writes one line to `runtime-feedback.jsonl`:
- `run_id`, `case_id` (subtask id), `model@thinking`
- `scoredBy`: list of verifier models that scored this run
- `verifierScore`: 0-10 overall score
- `success`, `hardGateHit`, `reworkTriggered`
- `taskVector`: weight vector used
- `latency_ms`, `cost_usd`, `ts`

This stream is append-only and accumulates one row per executed subtask.

### Stage 2: Capability telemetry (every run)

At run close, `update_capabilities.update_runtime_telemetry()` reads
`runtime-feedback.jsonl` and updates:
- `runtime.avgVerifyScore` (rolling average)
- `runtime.successRate`
- `runtime.samples` (cumulative count)
- `cost.avgInputTokens`, `cost.avgOutputTokens`, `cost.costPerCallCny`

This is **objective telemetry** (it does not change the capability scores
themselves), and is automatically applied without human review.

### Stage 3: Pending diff (every run)

`update_capabilities.pending_diff()` reads the latest feedback and computes a
proposed update to the capability scores (per dimension, per model@thinking).
The proposed update is written to `evals/pending-runtime-diff.json` with a
`baseHash` snapshot of the current capability archive. **No mutation of the
archive happens yet.**

### Stage 4: Apply (nightly, with health check)

The nightly `autoApply` job:
1. Loads the pending diff
2. Runs a drift health check: are the proposed changes within expected bounds
   (drift < some threshold over the past N days)?
3. If healthy: applies the diff, bumps `revision`, archives the previous
   revision to `capabilities.rev<N>.json.bak`
4. If unhealthy: alerts (the maintainer manually investigates)

## Why pending diffs?

- **Audit trail**: every change to the archive is reviewable
- **baseHash integrity**: if anything else (a benchmark ingest, a manual edit)
  changed the archive since the pending diff was computed, the diff is
  rejected at apply time — preventing two concurrent updates from clobbering
  each other
- **Roll-forward via revision history**: each revision snapshot is preserved,
  allowing post-hoc investigation of when capability scores changed and why

## What stays human-controlled

- Adding/removing models from the pool: manual (v15.5-K)
- Adding/editing benchmark cases: manual
- Changing the tier-bridge wire format: manual
- Archiving/promoting golden-set cases: manual (until v15.5 judge-recursion
  architecture is implemented)

## What auto-evolves

- Capability scores (per dimension, per model@thinking)
- Runtime telemetry (latency, cost, success rate)
- Provider health (circuit-breaker state, rolling failure rate)

## Consequences

**Positive**:
- The system stays current with provider changes
- Drift is detected within ~1 day (the nightly cycle)
- Audit trail is comprehensive

**Negative**:
- A misbehaving model (or a feedback loop with a hallucinating verifier) could
  poison the archive. Mitigated by the drift health check + baseHash integrity.
- The nightly job is a non-trivial piece of code that needs its own test
  coverage.