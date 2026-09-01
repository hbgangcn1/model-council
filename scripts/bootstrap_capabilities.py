#!/usr/bin/env python3
"""Bootstrap an empty capability archive and configuration in the current directory.

Usage:
    python scripts/bootstrap_capabilities.py

This creates:
- ./capabilities.json (empty archive, schemaVersion=2)
- ./council-params.json (from config/council-params.json.example.json)
- ./pricing.json (from config/pricing.json.example.json)
- ./pricing-profiles.json (from config/pricing-profiles.json.example.json)
- ./cost-tiers.json (from config/cost-tiers.json.example.json)
"""
import json
import shutil
import sys
from pathlib import Path


def bootstrap(workspace: Path = Path(".")):
    """Initialize capability archive and config files in `workspace`."""
    workspace = workspace.resolve()

    # 1. Empty capabilities.json
    caps_path = workspace / "capabilities.json"
    if caps_path.exists():
        print(f"  skip: {caps_path} already exists")
    else:
        caps_path.write_text(
            json.dumps(
                {
                    "schemaVersion": 2,
                    "revision": 0,
                    "generatedAt": None,
                    "updatedAt": None,
                    "meta": {"source": "bootstrap", "note": "Empty archive. Run benchmark to populate."},
                    "dimensions": [
                        {"id": "reasoning", "weight": 0.20},
                        {"id": "code", "weight": 0.15},
                        {"id": "chinese", "weight": 0.05},
                        {"id": "research", "weight": 0.15},
                        {"id": "instruction_following", "weight": 0.15},
                        {"id": "long_context", "weight": 0.10},
                        {"id": "tool_use", "weight": 0.10},
                        {"id": "creativity", "weight": 0.05},
                        {"id": "safety", "weight": 0.05},
                    ],
                    "models": {},
                    "ingestMeta": {},
                    "runtimeFeedback": {},
                },
                indent=2,
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )
        print(f"  + {caps_path}")

    # 2. Copy example configs (if not already present)
    repo_root = Path(__file__).resolve().parent.parent
    config_dir = repo_root / "config"
    for src_name, dst_name in [
        ("council-params.json.example.json", "council-params.json"),
        ("pricing.json.example.json", "pricing.json"),
        ("pricing-profiles.json.example.json", "pricing-profiles.json"),
        ("cost-tiers.json.example.json", "cost-tiers.json"),
    ]:
        src = config_dir / src_name
        dst = workspace / dst_name
        if dst.exists():
            print(f"  skip: {dst} already exists")
            continue
        if not src.exists():
            print(f"  warn: {src} not found, skipping")
            continue
        shutil.copy2(src, dst)
        print(f"  + {dst}")

    print()
    print("Workspace initialized.")
    print()
    print("Next steps:")
    print("  1. Edit council-params.json to tune behavior (optional).")
    print("  2. Edit pricing.json / pricing-profiles.json / cost-tiers.json")
    print("     with your provider pricing (required for cost-aware selection).")
    print("  3. Run benchmark to populate capabilities.json:")
    print("       python -m benchmark.bench.runner \\")
    print("         --cases benchmark/v21-cases.json \\")
    print("         --output benchmark/scores/")
    print("       python -m benchmark.capability_ingest --diff")
    print("       python -m benchmark.capability_ingest --apply")
    print("  4. Run your first council:")
    print("       python -m orchestrator.council_v14 --task 'Your question' --tier fast")


if __name__ == "__main__":
    target = Path(sys.argv[1]) if len(sys.argv) > 1 else Path(".")
    bootstrap(target)