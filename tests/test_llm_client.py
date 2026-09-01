"""Tests for the LLMClient abstraction (v15.6+).

These tests exercise the abstraction layer without actually contacting any
external LLM provider. We use stub/fake transports to verify:

- LLMClient protocol compliance
- BaseLLMClient retry + backoff behavior
- CompositeClient fallback chain semantics
- default_client() client selection from environment
"""
from __future__ import annotations

import os
import sys
import time
from pathlib import Path

import pytest

# Make orchestrator package importable when running pytest from repo root.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from orchestrator.llm_client import (  # noqa: E402
    BaseLLMClient,
    CallMeta,
    CompositeClient,
    LLMClient,
    LLMPermanentError,
    LLMRetryableError,
    default_client,
)
from orchestrator.llm_transports.stub import StubClient  # noqa: E402


# ===================== Protocol compliance =====================


def test_stub_client_is_llm_client():
    """StubClient satisfies the LLMClient protocol."""
    assert isinstance(StubClient(), LLMClient)


def test_base_llm_client_is_llm_client():
    """A minimal subclass of BaseLLMClient satisfies the protocol."""
    class Minimal(BaseLLMClient):
        def __init__(self):
            super().__init__(transport_name="minimal")

        def _do_call(self, model, thinking_level, prompt, max_tokens, tools=None):
            return "ok", CallMeta(transport="minimal").as_dict()

    assert isinstance(Minimal(), LLMClient)


def test_composite_client_is_llm_client():
    """CompositeClient satisfies the LLMClient protocol."""
    c = CompositeClient([StubClient()])
    assert isinstance(c, LLMClient)


# ===================== StubClient behavior =====================


def test_stub_client_raises_with_clear_message():
    """StubClient raises NotImplementedError-style error with config hint."""
    client = StubClient()
    with pytest.raises(LLMPermanentError) as exc_info:
        client.call_stream("test-model", "low", "test prompt", 100)
    assert "DSH_BRIDGE_URL" in str(exc_info.value)
    assert "OPENAI_COMPAT_BASE" in str(exc_info.value)


# ===================== BaseLLMClient retry behavior =====================


def test_base_client_no_retry_on_success():
    """Successful call: no retries, retry_count=0."""

    class OneShot(BaseLLMClient):
        def __init__(self):
            super().__init__(transport_name="oneshot")
            self.calls = 0

        def _do_call(self, model, thinking_level, prompt, max_tokens, tools=None):
            self.calls += 1
            return f"success for {model}", CallMeta(transport="oneshot").as_dict()

    client = OneShot()
    text, meta = client.call_stream("m", "low", "p", 100)
    assert text == "success for m"
    assert meta["retry_count"] == 0
    assert meta["transport"] == "oneshot"
    assert client.calls == 1


def test_base_client_retries_on_retryable_error():
    """Retryable errors trigger exponential backoff + retry."""

    class Flaky(BaseLLMClient):
        def __init__(self):
            super().__init__(
                transport_name="flaky",
                max_retries=3,
                retry_base_delay_s=0.01,  # fast tests
                retry_max_delay_s=0.05,
            )
            self.calls = 0

        def _do_call(self, model, thinking_level, prompt, max_tokens, tools=None):
            self.calls += 1
            if self.calls < 3:
                raise LLMRetryableError(f"flaky error {self.calls}")
            return "ok", CallMeta(transport="flaky").as_dict()

    client = Flaky()
    text, meta = client.call_stream("m", "low", "p", 100)
    assert text == "ok"
    assert meta["retry_count"] == 2  # 2 retries = 3 total
    assert client.calls == 3


def test_base_client_gives_up_after_max_retries():
    """After max_retries, raises LLMPermanentError."""

    class AlwaysFails(BaseLLMClient):
        def __init__(self):
            super().__init__(
                transport_name="always_fails",
                max_retries=2,
                retry_base_delay_s=0.01,
            )
            self.calls = 0

        def _do_call(self, model, thinking_level, prompt, max_tokens, tools=None):
            self.calls += 1
            raise LLMRetryableError(f"fail {self.calls}")

    client = AlwaysFails()
    with pytest.raises(LLMPermanentError) as exc_info:
        client.call_stream("m", "low", "p", 100)
    assert "retry" in str(exc_info.value).lower() or "RetryableError" in str(exc_info.value)
    assert client.calls == 3  # 1 initial + 2 retries


def test_base_client_no_retry_on_permanent_error():
    """Permanent errors (4xx) do not retry."""

    class Permanent(BaseLLMClient):
        def __init__(self):
            super().__init__(transport_name="permanent")
            self.calls = 0

        def _do_call(self, model, thinking_level, prompt, max_tokens, tools=None):
            self.calls += 1
            raise LLMPermanentError("HTTP 401: bad key", http_status=400)

    client = Permanent()
    with pytest.raises(LLMPermanentError):
        client.call_stream("m", "low", "p", 100)
    assert client.calls == 1  # no retry


def test_base_client_backoff_uses_retry_after():
    """If LLMRetryableError specifies retry_after_s, it's used (capped at max)."""

    class CustomBackoff(BaseLLMClient):
        def __init__(self):
            super().__init__(
                transport_name="custom",
                max_retries=1,
                retry_base_delay_s=10.0,
                retry_max_delay_s=20.0,
            )

        def _do_call(self, model, thinking_level, prompt, max_tokens, tools=None):
            raise LLMRetryableError("rate limited", retry_after_s=15.0)

    client = CustomBackoff()
    # Patch time.sleep to capture what delay is requested
    import orchestrator.llm_client as llm_module

    real_sleep = llm_module.time.sleep
    sleep_calls = []

    def fake_sleep(s):
        sleep_calls.append(s)

    llm_module.time.sleep = fake_sleep
    try:
        with pytest.raises(LLMPermanentError):
            client.call_stream("m", "low", "p", 100)
    finally:
        llm_module.time.sleep = real_sleep
    # One retry attempt with retry_after_s=15.0 (under cap of 20.0)
    assert sleep_calls == [15.0]


# ===================== CompositeClient fallback chain =====================


def test_composite_fallback_to_next_on_failure():
    """When first client fails, CompositeClient tries next."""

    class Fail(BaseLLMClient):
        def __init__(self):
            super().__init__(transport_name="fail")

        def _do_call(self, model, thinking_level, prompt, max_tokens, tools=None):
            raise LLMPermanentError("first always fails")

    class Ok(BaseLLMClient):
        def __init__(self):
            super().__init__(transport_name="ok")

        def _do_call(self, model, thinking_level, prompt, max_tokens, tools=None):
            return "second_ok", CallMeta(transport="ok").as_dict()

    success_models = []
    failure_models = []

    composite = CompositeClient(
        [Fail(), Ok()],
        on_success=lambda model, latency: success_models.append(model),
        on_failure=lambda model, error: failure_models.append(model),
    )
    text, meta = composite.call_stream("m", "low", "p", 100)
    assert text == "second_ok"
    assert success_models == ["m"]
    assert failure_models == ["m"]  # first client failed once


def test_composite_all_clients_fail():
    """When all clients fail, raises the last error."""

    class Fail(BaseLLMClient):
        def __init__(self, msg):
            super().__init__(transport_name="fail")
            self.msg = msg

        def _do_call(self, model, thinking_level, prompt, max_tokens, tools=None):
            raise LLMPermanentError(self.msg)

    composite = CompositeClient([Fail("a"), Fail("b"), Fail("c")])
    with pytest.raises(LLMPermanentError) as exc_info:
        composite.call_stream("m", "low", "p", 100)
    assert "c" in str(exc_info.value)  # last error mentioned


def test_composite_empty_clients_raises():
    """CompositeClient with zero clients is a config error."""
    with pytest.raises(ValueError):
        CompositeClient([])


def test_composite_callback_errors_swallowed():
    """Callback errors must not break the call."""

    class Ok(BaseLLMClient):
        def __init__(self):
            super().__init__(transport_name="ok")

        def _do_call(self, model, thinking_level, prompt, max_tokens, tools=None):
            return "ok", CallMeta(transport="ok").as_dict()

    def bad_callback(*args, **kwargs):
        raise RuntimeError("callback exploded")

    composite = CompositeClient(
        [Ok()],
        on_success=bad_callback,
        on_failure=bad_callback,
    )
    # Should not raise despite callback errors
    text, meta = composite.call_stream("m", "low", "p", 100)
    assert text == "ok"


# ===================== default_client() =====================


def test_default_client_includes_stub_by_default(monkeypatch):
    """Without any env vars, default_client() returns CompositeClient with at least
    the StubClient as the final fallback (DSHBridgeClient is always prepended
    for v15.6 backward compatibility — it resolves URL from env / credentials).
    """
    monkeypatch.delenv("DSH_BRIDGE_URL", raising=False)
    monkeypatch.delenv("OPENAI_COMPAT_BASE", raising=False)
    monkeypatch.delenv("ANTHROPIC_COMPAT_BASE", raising=False)
    c = default_client()
    assert isinstance(c, CompositeClient)
    assert len(c.clients) >= 1
    # The last client is always the stub (final fallback)
    assert isinstance(c.clients[-1], StubClient)


def test_default_client_with_dsh_bridge(monkeypatch):
    """With DSH_BRIDGE_URL set, default_client() prepends DSHBridgeClient."""
    monkeypatch.setenv("DSH_BRIDGE_URL", "http://example.com/api/council/llm-stream")
    monkeypatch.delenv("OPENAI_COMPAT_BASE", raising=False)
    monkeypatch.delenv("ANTHROPIC_COMPAT_BASE", raising=False)
    c = default_client()
    assert len(c.clients) == 2
    # First client should be DSH bridge
    from orchestrator.llm_transports.dsh_bridge import DSHBridgeClient
    assert isinstance(c.clients[0], DSHBridgeClient)
    # Last client is stub
    assert isinstance(c.clients[-1], StubClient)


def test_default_client_with_openai_compat(monkeypatch):
    """With OPENAI_COMPAT_BASE set, default_client() includes OpenAICompatClient."""
    monkeypatch.delenv("DSH_BRIDGE_URL", raising=False)
    monkeypatch.setenv("OPENAI_COMPAT_BASE", "https://api.deepseek.com/v1")
    monkeypatch.delenv("ANTHROPIC_COMPAT_BASE", raising=False)
    c = default_client()
    from orchestrator.llm_transports.openai_compat import OpenAICompatClient
    assert any(isinstance(client, OpenAICompatClient) for client in c.clients)


def test_default_client_with_anthropic_compat(monkeypatch):
    """With ANTHROPIC_COMPAT_BASE set, default_client() includes AnthropicCompatClient."""
    monkeypatch.delenv("DSH_BRIDGE_URL", raising=False)
    monkeypatch.delenv("OPENAI_COMPAT_BASE", raising=False)
    monkeypatch.setenv("ANTHROPIC_COMPAT_BASE", "https://api.example.com/anthropic")
    c = default_client()
    from orchestrator.llm_transports.anthropic_compat import AnthropicCompatClient
    assert any(isinstance(client, AnthropicCompatClient) for client in c.clients)


def test_default_client_priority_order(monkeypatch):
    """DSH > OpenAI > Anthropic > Stub (prepended in that order)."""
    monkeypatch.setenv("DSH_BRIDGE_URL", "http://dsh/api/council/llm-stream")
    monkeypatch.setenv("OPENAI_COMPAT_BASE", "https://api.deepseek.com/v1")
    monkeypatch.setenv("ANTHROPIC_COMPAT_BASE", "https://api.example.com/anthropic")
    c = default_client()
    assert len(c.clients) == 4
    # First is DSH, last is stub
    from orchestrator.llm_transports.dsh_bridge import DSHBridgeClient
    from orchestrator.llm_transports.anthropic_compat import AnthropicCompatClient
    from orchestrator.llm_transports.openai_compat import OpenAICompatClient
    assert isinstance(c.clients[0], DSHBridgeClient)
    assert any(isinstance(client, OpenAICompatClient) for client in c.clients[1:3])
    assert any(isinstance(client, AnthropicCompatClient) for client in c.clients[1:3])
    assert isinstance(c.clients[-1], StubClient)


def test_default_client_with_dsh_bridge_still_works(monkeypatch):
    """DSH_BRIDGE_URL takes precedence; the default DSHBridgeClient is still
    added (it resolves the URL from env / credentials)."""
    monkeypatch.setenv("DSH_BRIDGE_URL", "http://example.com/api/council/llm-stream")
    c = default_client()
    from orchestrator.llm_transports.dsh_bridge import DSHBridgeClient
    assert isinstance(c.clients[0], DSHBridgeClient)
    assert c.clients[0].url == "http://example.com/api/council/llm-stream"