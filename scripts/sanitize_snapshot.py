#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Export a publishable snapshot of maintainer capability data.

Reads the maintainer's live council directory (default: ~/.dsh/council) and
writes sanitized copies into this repository:

- capabilities.json — keeps model identity + scores/samples + cost/latency
  aggregates (these drive the selector out of the box); drops run-history
  linkage (_source_run_ids), timestamps, ingest metadata and runtime feedback.
- benchmark/golden/golden-set.json — copied verbatim (generic eval items).

Usage:
    python scripts/sanitize_snapshot.py [--council-dir DIR] [--repo DIR]

Run it before cutting a data-refresh release, inspect the diff, then commit.
"""
from __future__ import annotations

import argparse
import copy
import datetime
import json
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent

MODEL_KEEP = {
    "baseModel", "thinking", "provider", "vendorGroup", "tier",
    "stable", "identityUnknown",
}
DIM_KEEP = {"score", "samples", "freshness", "interpolated"}
RUNTIME_KEEP = {"avgVerifyScore", "successRate", "samples", "latencyP50Ms",
                "verifyScoreStd"}
COST_KEEP = {"avgInputTokens", "avgOutputTokens", "costPerCallCny"}


def _now_iso() -> str:
    return datetime.datetime.now(
        datetime.timezone(datetime.timedelta(hours=8))).isoformat()


def sanitize_capabilities(src: dict) -> tuple[dict, dict]:
    """Return (sanitized, stats)."""
    stats = {"models_in": 0, "models_out": 0, "dropped": []}
    out = {
        "schemaVersion": src.get("schemaVersion"),
        "revision": src.get("revision"),
        "generatedAt": _now_iso(),
        "updatedAt": _now_iso(),
        "_comment": ("Sanitized public snapshot: scores/samples/cost/latency kept; "
                     "run linkage, timestamps and local feedback dropped. "
                     "See scripts/sanitize_snapshot.py."),
    }
    if "dimensions" in src:
        out["dimensions"] = src["dimensions"]
    models = {}
    for key, m in (src.get("models") or {}).items():
        stats["models_in"] += 1
        if m.get("identityUnknown") or m.get("stable") is False:
            stats["dropped"].append(key)
            continue
        nm = {k: copy.deepcopy(m[k]) for k in MODEL_KEEP if k in m}
        caps = {}
        for dim, rec in (m.get("capabilities") or {}).items():
            caps[dim] = {k: rec[k] for k in DIM_KEEP if k in rec}
        nm["capabilities"] = caps
        if isinstance(m.get("runtime"), dict):
            nm["runtime"] = {k: m["runtime"][k] for k in RUNTIME_KEEP
                             if k in m["runtime"]}
        if isinstance(m.get("cost"), dict):
            nm["cost"] = {k: m["cost"][k] for k in COST_KEEP
                          if k in m["cost"]}
        models[key] = nm
        stats["models_out"] += 1
    out["models"] = models
    return out, stats


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--council-dir", default=str(Path.home() / ".dsh" / "council"))
    ap.add_argument("--repo", default=str(REPO))
    args = ap.parse_args()
    council = Path(args.council_dir)
    repo = Path(args.repo)

    src_caps = json.loads((council / "capabilities.json").read_text(encoding="utf-8"))
    caps, stats = sanitize_capabilities(src_caps)
    dst_caps = repo / "capabilities.json"
    dst_caps.write_text(json.dumps(caps, ensure_ascii=False, indent=2) + "\n",
                        encoding="utf-8")
    print(f"capabilities: {stats['models_in']} -> {stats['models_out']} models"
          + (f", dropped {stats['dropped']}" if stats["dropped"] else "")
          + f" -> {dst_caps}")

    src_golden = (council / "benchmark" / "golden" / "golden-set.json").read_text(
        encoding="utf-8").replace("\r\n", "\n")
    dst_golden = repo / "benchmark" / "golden" / "golden-set.json"
    dst_golden.write_text(src_golden if src_golden.endswith("\n") else src_golden + "\n",
                          encoding="utf-8")
    n_items = len(json.loads(src_golden).get("items", []))
    print(f"golden-set: {n_items} items -> {dst_golden}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
