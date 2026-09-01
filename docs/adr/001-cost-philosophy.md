# ADR-001: Cost Philosophy

**Status**: Accepted (2026-08-25)
**Version**: v15.4
**Supersedes**: ADR-pre-v15.4 (cost-termination guardrails)

## Context

Prior to v15.4, the orchestrator enforced a hard cost budget: if estimated cost
exceeded a tier-specific cap, the run was rejected before even starting. The
rationale was that runaway cost was a real concern with multi-model convergence
loops (each iteration calls multiple models).

In practice, this guardrail produced two failure modes:

1. **False rejects**: Tasks that *would* have produced valuable results were
   rejected because the *estimate* was pessimistic. The estimate was conservative
   on purpose (worst-case thinking + worst-case output), so it over-budgeted
   often.

2. **False sense of security**: When the estimate said "OK", users assumed cost
   was bounded. In reality, the actual cost could still exceed budget by 50-200%
   due to actual token usage exceeding estimates.

The v15.4 redesign also reconsidered the role of cost. Cost *does* matter — it
matters a lot when comparing models of equivalent capability. But cost should
*not* be the binding factor when it would force the system to pick a worse model.

## Decision

We adopt the following cost philosophy (v15.4):

1. **Cost selects, not caps.** Cost is one term in the selector's scoring
   function: `score = Σ dim_weight × capability − λ·cost − μ·latency + diversity`.
   Cost influences which model gets picked; it does *not* limit how many
   iterations the system runs.

2. **Runaway cost is bounded by structure, not by budget termination.**
   - `max-rounds` per tier (fast=3, standard=5, deep=6)
   - Dynamic wall-clock budget (v15.5): scales with task size, capped at host
     timeout − 240s margin
   - Currency exchange rate fallback (v15.5): three-tier fallback (primary CFETS
     → backup API → last successful rate); the system continues even when the
     rate is stale, with a `fx_warning` in the run result

3. **Pre-flight reports, not rejects.** Before a run starts, the budget module
   reports the expected cost range based on balance snapshot and per-model
   average. The orchestrator continues either way; the user can choose to abort
   if the report is unexpectedly high.

4. **Balance-awareness, not balance-cap.** When the user's balance is critically
   low (< 10% of estimated usage), the selector's `balance_exhausted` guardrail
   fires and excludes the affected providers from selection. Other providers
   remain eligible.

## Consequences

**Positive**:
- No more false rejects on legitimate tasks
- Cost is communicated (report) but not enforced (terminator)
- The selector naturally prefers cheaper models when capability is tied

**Negative**:
- A misconfigured task (e.g. accidentally decomposing into 100 subtasks) can rack
  up real cost. The dynamic wall-clock + max-rounds bound the worst case but
  do not eliminate it.
- Users must read the `fx_warning` and pre-flight `cost_report` fields; the
  system no longer refuses on their behalf.

## Alternatives considered

- **Keep the budget cap, but make it higher**: rejected. The fundamental issue
  is that estimates are wrong; raising the cap doesn't fix that.
- **Hard cap per provider, soft cap per run**: rejected. Too complex; the
  per-provider granularity isn't meaningful when a single model call can
  consume 50% of a small balance.
- **Pure cost-blind selection**: rejected. Cost is a real signal of efficiency;
  ignoring it would over-favor expensive-but-good models on tasks where a
  cheaper-but-adequate model exists.