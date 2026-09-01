# ADR-002: Vendor Grouping

**Status**: Accepted (planned for v15.5, implemented in v15.6)
**Version**: v15.5+

## Context

Cross-verification is the core of Model Council's reliability story: a model
should never verify its own output, and ideally should be from a different
provider/vendor to maximize independence.

Prior to v15.5, the "different vendor" constraint was implemented as
"different `baseModel`" — i.e. `deepseek-v4-pro` and `deepseek-v4-flash` were
treated as different models (and thus eligible to verify each other). But they
are *both* DeepSeek models, share the same training pipeline, the same
fine-tuning methodology, and arguably exhibit correlated failure modes. This was
discovered during the v15.5 meta-review when a self-critique run produced
`deepseek-v4-pro__off` and `deepseek-v4-flash__off` as the verifier pair — same
vendor, different baseModel.

## Decision

We introduce an explicit `vendorGroup` field on every `model@thinking` entry
in the capability archive. The selector enforces cross-vendor verification at
the `vendorGroup` level, not the `baseModel` level.

The `vendorGroup` is currently inferred from the `provider` field:

```python
def _vendor_of_cand(cand):
    vg = cand.get("vendorGroup")
    if vg:
        return vg
    p = cand.get("provider") or ""
    if p == "deepseek-official":
        return "deepseek"
    if p == "minimax-cn":
        return "minimax"
    return p or "unknown"
```

Examples:
- `deepseek-v4-pro` and `deepseek-v4-flash` both have `provider=deepseek-official`
  → both in `vendorGroup=deepseek` → cannot verify each other
- `deepseek-v4-pro` and `MiniMax-M3` → different vendors → can verify each other

## Consequences

**Positive**:
- Cross-verification actually means cross-vendor (the original intent)
- The selector can detect "vendor monoculture" — if the capability archive has
  only one vendor at a given thinking level, the selector flags it as a
  coverage gap and recommends adding another vendor

**Negative**:
- Smaller candidate pool for cross-vendor verification — if you only have
  DeepSeek models, you cannot run cross-vendor verification at all
- The `vendorGroup` taxonomy must be maintained as new providers are added

## Implementation notes

- `vendorGroup` is stored alongside `provider` in `capabilities.json` (the
  archive schema; not committed)
- The selector's `vendorGroup` constraint is enforced *before* capability
  scoring: a candidate is excluded if `vendorGroup` matches the executor's
  `vendorGroup`
- The `audit_council_orchestrator.py` script verifies that the orchestrator
  (Python) enforces this; the LLM-side tool registry does not need to know
  about `vendorGroup`