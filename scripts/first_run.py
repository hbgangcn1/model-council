#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""First-run wizard: verify install, credentials and data in one go.

Usage:
    python scripts/first_run.py [--init-credentials]

Without flags it only checks and reports (exits non-zero with actionable
instructions when something is missing). With --init-credentials it prompts
for API keys (hidden input) and writes ~/.model-council/credentials (0600).

Exit codes: 0 = ready to run councils; 1 = environment issue; 2 = no credentials.
"""
from __future__ import annotations

import json
import os
import stat
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

KEY_NAMES = ("DEEPSEEK_API_KEY", "MINIMAX_CN_API_KEY")
CRED_PATH = Path.home() / ".model-council" / "credentials"


def fail(msg: str, code: int = 1) -> int:
    print(f"[first-run] MISSING: {msg}")
    return code


def main() -> int:
    print(f"[first-run] repo: {REPO}")
    if sys.version_info < (3, 10):
        return fail(f"python >= 3.10 required, found {sys.version.split()[0]}")

    # ---- 1. data snapshot ----
    try:
        caps = json.loads((REPO / "capabilities.json").read_text(encoding="utf-8"))
        golden = json.loads((REPO / "benchmark" / "golden" / "golden-set.json")
                            .read_text(encoding="utf-8"))
        cases = json.loads((REPO / "benchmark" / "v21-cases.json").read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as e:
        return fail(f"data files unreadable: {e}")
    n_models = len(caps.get("models", {}))
    n_golden = len(golden.get("items", []))
    n_cases = len(cases.get("cases", cases) if isinstance(cases, dict) else cases)
    print(f"[first-run] data: {n_models} capability entries (rev {caps.get('revision')}), "
          f"{n_golden} golden items, {n_cases} bench cases")
    if n_models < 3 or n_golden < 3:
        return fail("snapshot looks empty; re-clone or re-run sanitize_snapshot.py")

    # ---- 2. credentials ----
    from orchestrator.config_loader import api_keys
    try:
        keys = api_keys()
        have = [k for k in KEY_NAMES if keys.get(k)]
        print(f"[first-run] credentials: found ({', '.join(sorted(keys)) or 'none of the LLM keys'})")
    except FileNotFoundError as e:
        if "--init-credentials" not in sys.argv:
            print(f"[first-run] no credentials: {e}")
            print("[first-run] fastest fix, pick one:")
            print("  export DEEPSEEK_API_KEY=... MINIMAX_CN_API_KEY=...")
            print("  python scripts/first_run.py --init-credentials   # writes ~/.model-council/credentials")
            return 2
        import getpass
        CRED_PATH.parent.mkdir(parents=True, exist_ok=True)
        lines = []
        for name in KEY_NAMES:
            val = getpass.getpass(f"{name} (empty to skip): ").strip()
            if val:
                lines.append(f"{name}: {val}")
        if not lines:
            return fail("no keys entered, nothing written", 2)
        CRED_PATH.write_text("\n".join(lines) + "\n", encoding="utf-8")
        try:
            CRED_PATH.chmod(stat.S_IRUSR | stat.S_IWUSR)
        except OSError:
            pass
        print(f"[first-run] wrote {CRED_PATH} (owner-only)")
        keys = api_keys()

    # ---- 3. offline smoke (no money) ----
    try:
        import pytest  # noqa: F401
        r = subprocess.run([sys.executable, "-m", "pytest", "tests/test_llm_client.py",
                            "orchestrator/test_v158.py", "-q"],
                           cwd=REPO, capture_output=True, text=True, timeout=300)
        tail = (r.stdout + r.stderr).strip().splitlines()
        print("[first-run] pytest:", tail[-1] if tail else f"exit={r.returncode}")
        if r.returncode != 0:
            return fail("offline tests failed; see above")
    except (ImportError, OSError):
        print("[first-run] pytest not installed, skipping (pip install pytest to enable)")
    r = subprocess.run([sys.executable, "-m", "orchestrator.judge_drift", "--dry"],
                       cwd=REPO, capture_output=True, text=True, timeout=120)
    if r.returncode != 0:
        return fail("judge --dry failed")
    try:
        items = json.loads(r.stdout).get("items")
        print(f"[first-run] judge --dry: ok ({items} golden items, no API cost)")
    except (json.JSONDecodeError, AttributeError):
        print("[first-run] judge --dry: ok")

    print("[first-run] READY. Next:")
    print("  python -m orchestrator.council_v14 --task 'Your question' --tier fast   # first real council (~¥0.03)")
    print("  ./scripts/first-bench.sh   # full benchmark when you want your own scores (1-2h, ~$15-25)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
