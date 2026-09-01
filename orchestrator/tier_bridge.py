"""Tier-bridge stub for the public release.

This is a minimal stub that lets the orchestrator import and run end-to-end
without the optional host-side tier-bridge plugin. It provides:

- `wire_for(model, level)`: returns a sensible default wire-format dict for
  thinking/reasoning parameters.
- `max_tokens_for(model, which)`: returns a sane default max_tokens value.

**Production note**: this stub does NOT query a live model catalog. For
production use, replace this file with your own bridge plugin that talks to
your model registry (or use a host-side integration like the DSH
host-bridge plugin). See docs/tier-alignment.md for the bridge contract.

The tier-bridge contract requires these two functions:

- wire_for(model, level): return the wire-format dict for the model at the
  given thinking level (e.g. {"thinking": {"type": "enabled"},
  "reasoning_effort": "high"}).
- max_tokens_for(model, which): return max_tokens. which is either
  'defaultMaxTokens' (the request default) or 'capabilityMaxTokens' (the
  model's output capability upper bound).
"""
from __future__ import annotations

# Default thinking/reasoning wire formats by family.
# Real implementations read this from a live model catalog.
DEFAULT_WIRE_FAMILIES = {
    "deepseek": {
        "off": {"thinking": {"type": "disabled"}},
        "low": {"thinking": {"type": "enabled"}, "reasoning_effort": "low"},
        "high": {"thinking": {"type": "enabled"}, "reasoning_effort": "high"},
        "max": {"thinking": {"type": "enabled"}, "reasoning_effort": "max"},
    },
    "minimax": {
        # pi-ai anthropic-style interface for MiniMax
        "off": {"thinking": {"type": "disabled"}},
        "minimal": {"thinking": {"type": "enabled", "budget_tokens": 512}},
        "low": {"thinking": {"type": "enabled", "budget_tokens": 2048}},
        "medium": {"thinking": {"type": "enabled", "budget_tokens": 8192}},
        "high": {"thinking": {"type": "enabled", "budget_tokens": 16384}},
    },
}

DEFAULT_MAX_TOKENS = {
    "deepseek-v4-pro": 256000,
    "deepseek-v4-flash": 256000,
    "MiniMax-M3": 131072,
    "MiniMax-M2.7": 131072,
    "glm-5.3-flash": 131072,
    "stealth/ox-alpha": 131072,
}


def _family(model: str) -> str:
    """Return the family name for a model based on prefix matching."""
    m = model.lower()
    if m.startswith("deepseek"):
        return "deepseek"
    if m.startswith("minimax") or m.startswith("MiniMax"):
        return "minimax"
    return "minimax"  # default fallback


def wire_for(model: str, level: str) -> dict:
    """Return the wire-format dict for model at thinking level."""
    family = DEFAULT_WIRE_FAMILIES.get(_family(model), DEFAULT_WIRE_FAMILIES["minimax"])
    if level in family:
        return family[level]
    return {"thinking": {"type": "enabled", "level": level}}


def max_tokens_for(model: str, which: str = "defaultMaxTokens") -> int:
    """Return max_tokens for the model.

    For unknown models, returns 8192 (default) or 16384 (capability), which
    covers most thinking-budget scenarios without being excessive.
    """
    if which == "capabilityMaxTokens":
        return DEFAULT_MAX_TOKENS.get(model, 16384)
    return DEFAULT_MAX_TOKENS.get(model, 8192)