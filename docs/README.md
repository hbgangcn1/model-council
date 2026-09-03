# Documentation

This directory contains the design documentation for Model Council.

## Reading order

| # | Document | Purpose |
|---|---|---|
| 0 | [operations.md](operations.md) | Run it daily: ops chain, cron, data files, troubleshooting (start here to operate) |
| 1 | [architecture.md](architecture.md) | High-level system architecture, role flow, file layout |
| 2 | [design-decisions.md](design-decisions.md) | Full design document (v15.6 baseline, every decision since v14) |
| 3 | [roadmap.md](roadmap.md) | Active roadmap (v15.5 → next) |
| 4 | [tier-alignment.md](tier-alignment.md) | Tier-bridge / wire-format alignment plan |
| 5 | [meta-review.md](meta-review.md) | Most recent self-review of the system |
| 6 | [examples/basic-usage.md](examples/basic-usage.md) | How to run your first council |
| 7 | [examples/custom-prompts.md](examples/custom-prompts.md) | How to customize the decomposer / verifier / synth prompts |
| 8 | [adr/](adr/) | Architecture Decision Records (the *why* behind each major decision) |

## Architecture Decision Records (ADRs)

Each ADR captures one significant design decision: the context, the choice made,
the alternatives considered, and the consequences. We keep these terse — the goal
is to explain *why*, not *what* (the *what* lives in code).

- [ADR-001: Cost philosophy](adr/001-cost-philosophy.md) — v15.4 cost philosophy:
  cost selects among equivalent-capability models; it does not cap usage
- [ADR-002: Vendor grouping](adr/002-vendor-grouping.md) — verifier must be from
  a different vendor than the executor; models with the same `baseModel` but
  different `provider` may still be in the same vendor group
- [ADR-003: Self-evolve loop](adr/003-self-evolve.md) — every run writes one
  feedback row; nightly job rolls signals back into capability archive under
  maintainer-approved pending diffs

## What this documentation does NOT cover

- The capability archive itself (`capabilities.json`) — that is your data, not
  documentation. The schema is documented in [architecture.md](architecture.md#capability-archive-schema).
- The benchmark cases (`benchmark/v21-cases.json`) — those are test data.
- The default pricing profile (`config/pricing.json.example.json`) — that is a
  template you must populate with your own provider pricing.