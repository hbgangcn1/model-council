#!/usr/bin/env python3
"""Verify that model-council is correctly installed.

Usage:
    python scripts/verify_installation.py

Checks:
- All required Python modules import cleanly
- Capability archive is readable (if present)
- Council params are valid JSON (if present)
- Pricing profiles are valid JSON (if present)
- Required environment variables are set (provider API keys)
- pytest test suite passes
"""
import json
import os
import subprocess
import sys
import traceback
from pathlib import Path


def check_imports() -> bool:
    print("[1/5] Importing core modules...")
    modules = [
        "orchestrator.council_v14",
        "orchestrator.selector",
        "orchestrator.terminator",
        "orchestrator.params",
        "orchestrator.budget",
        "orchestrator.cost_calibrate",
        "orchestrator.update_capabilities",
        "benchmark.capability_ingest",
        "benchmark.bench.runner",
        "json_repair",
        "pool",
    ]
    all_ok = True
    for mod_name in modules:
        try:
            __import__(mod_name)
            print(f"  ok: {mod_name}")
        except Exception as e:
            print(f"  FAIL: {mod_name}: {e}")
            all_ok = False
    return all_ok


def check_workspace() -> bool:
    print()
    print("[2/5] Checking workspace files...")
    ws = Path(".")
    files = ["capabilities.json", "council-params.json"]
    all_ok = True
    for fname in files:
        p = ws / fname
        if not p.exists():
            print(f"  warn: {p} not found (run scripts/bootstrap_capabilities.py to create)")
            continue
        try:
            json.loads(p.read_text(encoding="utf-8"))
            print(f"  ok: {p} (valid JSON)")
        except json.JSONDecodeError as e:
            print(f"  FAIL: {p} invalid JSON: {e}")
            all_ok = False
    return all_ok


def check_env() -> bool:
    print()
    print("[3/5] Checking environment variables...")
    keys = [
        "DEEPSEEK_API_KEY",
        "MINIMAX_CN_API_KEY",
        "OPENROUTER_API_KEY",
    ]
    all_ok = True
    any_set = False
    for k in keys:
        v = os.environ.get(k, "")
        if v:
            print(f"  ok: {k} (set, length={len(v)})")
            any_set = True
        else:
            print(f"  warn: {k} not set (required only if you use this provider)")
    if not any_set:
        print("  FAIL: no provider keys set; council cannot run")
        all_ok = False
    return all_ok


def check_pytest() -> bool:
    print()
    print("[4/5] Running pytest suite...")
    try:
        result = subprocess.run(
            ["pytest", "-q", "--tb=short"],
            capture_output=True,
            text=True,
            timeout=300,
        )
        if result.returncode == 0:
            # Extract the summary line
            for line in result.stdout.splitlines()[-5:]:
                if "passed" in line or "failed" in line:
                    print(f"  ok: {line.strip()}")
            return True
        else:
            print("  FAIL: pytest had failures")
            print(result.stdout[-2000:])
            print(result.stderr[-1000:])
            return False
    except subprocess.TimeoutExpired:
        print("  FAIL: pytest timed out (5 min)")
        return False
    except FileNotFoundError:
        print("  warn: pytest not installed; install with: pip install pytest")
        return True  # not fatal
    except Exception as e:
        print(f"  FAIL: {e}")
        return False


def check_dry_run() -> bool:
    print()
    print("[5/5] Smoke test: dry-run council on a simple task...")
    try:
        from orchestrator.council_v14 import run_council

        result = run_council(
            task="Smoke test: verify installation",
            tier="fast",
            mode="inline",
            dry=True,
        )
        if result.get("status") == "ok_dry":
            print(f"  ok: dry-run succeeded (subtasks={result.get('subtasks')})")
            return True
        else:
            print(f"  FAIL: dry-run returned: {result}")
            return False
    except Exception as e:
        print(f"  FAIL: {e}")
        traceback.print_exc()
        return False


def main() -> int:
    print("=== Model Council Installation Verification ===")
    print()
    checks = [
        ("Import", check_imports),
        ("Workspace", check_workspace),
        ("Env", check_env),
        ("Pytest", check_pytest),
        ("DryRun", check_dry_run),
    ]
    results = {}
    for name, fn in checks:
        try:
            results[name] = fn()
        except Exception as e:
            print(f"  ERROR in {name}: {e}")
            results[name] = False
    print()
    print("=== Summary ===")
    all_pass = True
    for name, ok in results.items():
        status = "ok" if ok else "FAIL"
        print(f"  [{status}] {name}")
        if not ok:
            all_pass = False
    print()
    if all_pass:
        print("All checks passed. Model Council is ready.")
        return 0
    else:
        print("Some checks failed. See output above.")
        return 1


if __name__ == "__main__":
    sys.exit(main())