"""Smoke tests for dev/bridge.py — verify clean checkout doesn't crash.

Contract verification: bridge.py exposes all_candidates / wire_for /
max_tokens_for / levels_for / model_entry / vendor_group with the same
shapes as the runtime ~/.dsh/council/bridge.py. Imports must succeed in
a clean checkout (pip install -e .) without depending on the runtime path.

These tests intentionally avoid touching ~/.dsh/council/; they monkeypatch
bridge.RUNTIME_BRIDGE and bridge.DEV_FIXTURE module attributes (Path.home()
is evaluated at import time, so env var monkeypatching is too late).
"""
import json
import tempfile
from pathlib import Path

import pytest

import bridge

DEV_REPO_ROOT = Path(__file__).resolve().parent.parent  # tests/../ = repo root
DEV_FIXTURE = DEV_REPO_ROOT / "benchmark" / "test-fixtures" / "model-tier-bridge.json"


@pytest.fixture
def no_runtime_bridge(monkeypatch):
    """Force dev fallback by pointing RUNTIME_BRIDGE at a nonexistent path.

    bridge.RUNTIME_BRIDGE = Path.home() / ... is evaluated at import time,
    so HOME env-var monkeypatching has no effect — we must rebind the
    module attribute directly.
    """
    fake_root = Path(tempfile.mkdtemp())
    fake_path = fake_root / "no-runtime-bridge.json"
    assert not fake_path.exists()
    monkeypatch.setattr(bridge, "RUNTIME_BRIDGE", fake_path)
    monkeypatch.setattr(bridge, "_cache", None)
    yield fake_root


def test_bridge_module_exposes_full_contract():
    """All 7 entry points runtime bridge exposes are present here."""
    for name in ("all_candidates", "wire_for", "max_tokens_for",
                 "levels_for", "model_entry", "vendor_group", "load"):
        assert hasattr(bridge, name), f"bridge.{name} missing"


def test_clean_checkout_dev_fixture_is_loadable():
    """Dev fixture exists and yields ≥ 1 (model, level) pair.

    This is the smoke test that mirrors the user-facing scenario:
    clone model-council repo, pip install -e ., run benchmark.
    """
    assert DEV_FIXTURE.exists(), (
        f"Dev fixture missing at {DEV_FIXTURE} — bridge.py fallback chain "
        "will fail-loud on clean checkout (by design)"
    )
    bridge._cache = None
    cands = bridge.all_candidates()
    assert len(cands) >= 4  # dev-test-model-a (3 levels) + dev-test-model-b (2 levels) = 5
    assert all(isinstance(c, tuple) and len(c) == 2 for c in cands)


def test_bridge_falls_back_to_dev_fixture_when_runtime_missing(no_runtime_bridge):
    """RUNTIME_BRIDGE points at nonexistent file → dev fixture picked up."""
    cands = bridge.all_candidates()
    models = {m for m, _ in cands}
    assert models == {"dev-test-model-a", "dev-test-model-b"}

    levels = bridge.levels_for("dev-test-model-a")
    assert levels == ["off", "low", "high"]

    wire = bridge.wire_for("dev-test-model-a", "high")
    assert isinstance(wire, dict)
    assert wire["max_tokens"] == 8192
    assert wire["temperature"] == 0.7

    mt = bridge.max_tokens_for("dev-test-model-a")
    assert mt == 8192
    assert bridge.vendor_group("dev-test-model-a") == "dev-vendor-a"


def test_bridge_fail_loud_when_no_source_anywhere(monkeypatch, no_runtime_bridge):
    """PATH_BRIDGE_FILE unset + RUNTIME_BRIDGE nowhere + DEV_FIXTURE hidden → RuntimeError."""
    monkeypatch.delenv("PATH_BRIDGE_FILE", raising=False)
    # Hide dev fixture too (no_runtime_bridge only handles RUNTIME_BRIDGE)
    monkeypatch.setattr(bridge, "DEV_FIXTURE", no_runtime_bridge / "no_fixture.json")
    monkeypatch.setattr(bridge, "_cache", None)
    with pytest.raises(RuntimeError, match="缺 model-tier-bridge.json"):
        bridge.load()


def test_bridge_path_override_takes_precedence(monkeypatch, tmp_path):
    """PATH_BRIDGE_FILE beats both runtime and dev fixture."""
    custom = tmp_path / "custom.json"
    custom.write_text(json.dumps({
        "models": {
            "custom-model": {
                "vendorGroup": "custom-vendor",
                "capabilityMaxTokens": 1024,
                "defaultMaxTokens": 512,
                "levels": [{"level": "off", "wire": {"max_tokens": 256}}]
            }
        }
    }), encoding="utf-8")
    monkeypatch.setenv("PATH_BRIDGE_FILE", str(custom))
    monkeypatch.setattr(bridge, "_cache", None)

    cands = bridge.all_candidates()
    models = {m for m, _ in cands}
    assert models == {"custom-model"}
    assert bridge.vendor_group("custom-model") == "custom-vendor"
    assert bridge.max_tokens_for("custom-model") == 1024
    assert bridge.wire_for("custom-model", "off")["max_tokens"] == 256


def test_bridge_unknown_model_raises_value_error(monkeypatch):
    """Lookup of a model not in any loaded bridge fails loud."""
    fake_root = Path(tempfile.mkdtemp())
    monkeypatch.setattr(bridge, "RUNTIME_BRIDGE", fake_root / "nonexistent.json")
    monkeypatch.setattr(bridge, "_cache", None)
    with pytest.raises(ValueError, match="不在 DSH 档位桥中"):
        bridge.model_entry("nonexistent-model-xyz")


def test_bridge_unknown_level_raises_value_error(no_runtime_bridge):
    """Lookup of a valid model but invalid level fails loud."""
    # no_runtime_bridge fixture forces dev fixture to be loaded
    with pytest.raises(ValueError, match="无档位"):
        bridge.wire_for("dev-test-model-a", "no-such-level")


def test_bridge_load_is_cached(no_runtime_bridge):
    """Subsequent calls reuse cached doc (parity with runtime _cache)."""
    d1 = bridge.load()
    d2 = bridge.load()
    assert d1 is d2  # same object — proves caching


def test_clean_checkout_runner_does_not_crash_on_import(no_runtime_bridge):
    """Simulate the actual user scenario: dev checkout + benchmark.bench.config import.

    We don't invoke the runner (would need network/credentials); instead we
    verify that the exact import chain that breaks on a clean checkout works.
    """
    # Replay config.py's sys.path + import dance, then touch the functions config.py calls
    bridge._cache = None
    # Mirrors: COUNCIL = BASE.parent; sys.path.insert(0, str(COUNCIL))
    # We're testing dev, so COUNCIL == our repo root which is already on sys.path
    # (pytest runs from repo root, OR pip install -e . puts it there)
    from benchmark.bench import config  # ← the actual offender in production
    cands = config.CANDIDATES
    assert isinstance(cands, list)
    assert all(isinstance(c, tuple) and len(c) == 2 for c in cands)
    # wire_for / max_tokens_for / thinking_param from config.py must all work
    sample_model, sample_level = cands[0]
    assert isinstance(config.max_tokens_for(sample_model), int)
    assert isinstance(config.thinking_param(sample_model, sample_level), dict)
