"""LLM Client abstraction (v15.6+).

The orchestrator talks to LLMs through an `LLMClient` protocol, so that the
transport layer (DSH bridge / OpenAI-compat HTTP / Anthropic-compat HTTP / stub)
is interchangeable. This lets:

- DSH-hosted users transparently route through `~/.dsh/profiles/web/node_modules/
  dsh-council/index.js`'s `/api/council/llm-stream` endpoint.
- Public-release users run with any transport that exposes `LLMClient`
  (OpenAI-compat, Anthropic-compat, or just a stub that fails clearly).
- The selector / orchestrator code paths stay unchanged.

Design (v15.6):
- `LLMClient`: Protocol with `call_stream(model, level, prompt, max_tokens, tools?)`.
- `BaseLLMClient`: provides retry + quota handling + circuit-breaker integration.
- `CompositeClient`: chains multiple `LLMClient`s with fallback + success/failure
  callbacks (used to wire into `selector.record_success/record_failure`).
- `default_client()`: returns a `CompositeClient` whose primary client is the
  StubClient (clear NotImplementedError). Other transports are prepended in
  priority order if their env vars are set:

      DSH_BRIDGE_URL=http://127.0.0.1:3080/api/council/llm-stream  → DSHBridgeClient
      OPENAI_COMPAT_BASE=https://api.deepseek.com/v1
        + OPENAI_COMPAT_KEY=...                                       → OpenAICompatClient
      ANTHROPIC_COMPAT_BASE=https://api.minimaxi.com/anthropic
        + ANTHROPIC_COMPAT_KEY=...                                    → AnthropicCompatClient

  When a request succeeds, the configured `on_success` callback fires
  (default: `selector.record_success(model)`). When all clients fail,
  `on_failure` fires for each tried client (default:
  `selector.record_failure(model)`).
"""
from __future__ import annotations

import os
import time
import traceback
from dataclasses import dataclass, field
from typing import Optional, Protocol, runtime_checkable


# ===================== Public Protocol =====================


@runtime_checkable
class LLMClient(Protocol):
    """LLM Client protocol.

    Implementations must define `call_stream` returning `(text, meta)`. Meta
    must contain at least: `finish_reason`, `usage`, `elapsed_s`. Optional:
    `timeout_kind`, `http_status`, `retry_count`, `transport` (name of the
    transport that handled the request).
    """

    def call_stream(
        self,
        model: str,
        thinking_level: str,
        prompt: str,
        max_tokens: int,
        tools: Optional[list] = None,
    ) -> tuple[str, dict]:
        ...


# ===================== Common result types =====================


@dataclass
class CallMeta:
    """Standardized metadata for LLM calls.

    `usage` keys: promptTokens / completionTokens / cacheHitTokens
    (matches v15.3+ cost_calibrate accounting).
    """

    finish_reason: str = "stop"
    elapsed_s: float = 0.0
    usage: dict = field(default_factory=dict)
    timeout_kind: Optional[str] = None
    http_status: Optional[int] = None
    retry_count: int = 0
    transport: str = "unknown"
    error: Optional[str] = None

    def as_dict(self) -> dict:
        d = {
            "finish_reason": self.finish_reason,
            "elapsed_s": self.elapsed_s,
            "usage": self.usage,
            "retry_count": self.retry_count,
            "transport": self.transport,
        }
        if self.timeout_kind is not None:
            d["timeout_kind"] = self.timeout_kind
        if self.http_status is not None:
            d["http_status"] = self.http_status
        if self.error is not None:
            d["error"] = self.error
        return d


# ===================== Retryable error types =====================


class LLMRetryableError(Exception):
    """Raised by transports for 429 / 5xx errors that should trigger backoff."""

    def __init__(self, message: str, retry_after_s: Optional[float] = None):
        super().__init__(message)
        self.retry_after_s = retry_after_s


class LLMPermanentError(Exception):
    """Raised by transports for 4xx errors (other than 429) that should NOT retry."""

    def __init__(self, message: str, http_status: Optional[int] = None):
        super().__init__(message)
        self.http_status = http_status


class LLMQuotaExhaustedError(LLMPermanentError):
    """额度用尽：余额/配额耗尽导致的限流，不应重试。

    挂在 LLMPermanentError 下，所以 BaseLLMClient 的 except LLMPermanentError
    分支天然直接上抛、零重试；CompositeClient 会继续试下一个 transport，
    但同属配额耗尽的 vendor 会同样快速失败，不会烧时间。
    下游靠消息前缀 "QUOTA_EXHAUSTED" 识别（见 dsh_bridge 分类器）。
    """


# ===================== Base class with retry + circuit breaker =====================


class BaseLLMClient:
    """Base class for LLMClient implementations.

    Provides:
    - Retry loop with exponential backoff (default 3 attempts, 10s base, 60s max)
    - Circuit breaker integration: success / failure callbacks
    - Standardized error → meta mapping

    Subclasses must implement `_do_call`. They should raise `LLMRetryableError` for
    transient failures (429, 5xx, network) and `LLMPermanentError` for
    non-retryable errors (400, 401, 403, etc.).
    """

    DEFAULT_MAX_RETRIES = 3
    DEFAULT_RETRY_BASE_DELAY_S = 10.0
    DEFAULT_RETRY_MAX_DELAY_S = 60.0

    def __init__(
        self,
        transport_name: str,
        max_retries: int = DEFAULT_MAX_RETRIES,
        retry_base_delay_s: float = DEFAULT_RETRY_BASE_DELAY_S,
        retry_max_delay_s: float = DEFAULT_RETRY_MAX_DELAY_S,
    ):
        self.transport_name = transport_name
        self.max_retries = max_retries
        self.retry_base_delay_s = retry_base_delay_s
        self.retry_max_delay_s = retry_max_delay_s

    def call_stream(
        self,
        model: str,
        thinking_level: str,
        prompt: str,
        max_tokens: int,
        tools: Optional[list] = None,
    ) -> tuple[str, dict]:
        """Call _do_call with retry + backoff."""
        attempt = 0
        last_error = None
        t0 = time.time()
        retry_count = 0
        while attempt <= self.max_retries:
            try:
                text, meta = self._do_call(model, thinking_level, prompt, max_tokens, tools)
                meta = dict(meta)
                # BaseLLMClient owns retry_count / transport / elapsed_s.
                # Transports should not set these (we override them here).
                meta["retry_count"] = retry_count
                meta["transport"] = self.transport_name
                meta["elapsed_s"] = round(time.time() - t0, 1)
                return text, meta
            except LLMRetryableError as e:
                last_error = e
                if attempt >= self.max_retries:
                    break
                delay = self._compute_backoff(attempt, getattr(e, "retry_after_s", None))
                time.sleep(delay)
                attempt += 1
                retry_count += 1
            except LLMPermanentError as e:
                # Non-retryable: fail immediately
                raise
            except Exception as e:
                # Unexpected: treat as retryable
                last_error = e
                if attempt >= self.max_retries:
                    break
                delay = self._compute_backoff(attempt, None)
                time.sleep(delay)
                attempt += 1
                retry_count += 1

        # All retries exhausted
        err_msg = f"{type(last_error).__name__}: {str(last_error)[:300]}" if last_error else "unknown"
        meta = CallMeta(
            finish_reason="error",
            elapsed_s=round(time.time() - t0, 1),
            timeout_kind="retries_exhausted",
            retry_count=retry_count,
            transport=self.transport_name,
            error=err_msg,
        ).as_dict()
        # Re-raise the last error so CompositeClient can try the next client
        raise LLMPermanentError(err_msg) from last_error

    def _do_call(
        self,
        model: str,
        thinking_level: str,
        prompt: str,
        max_tokens: int,
        tools: Optional[list] = None,
    ) -> tuple[str, dict]:
        """Subclasses override this. Default raises NotImplementedError."""
        raise NotImplementedError(f"{self.__class__.__name__}._do_call not implemented")

    def _compute_backoff(self, attempt: int, retry_after_s: Optional[float]) -> float:
        """Exponential backoff: base * 2^attempt, capped at max."""
        if retry_after_s is not None:
            return min(retry_after_s, self.retry_max_delay_s)
        delay = self.retry_base_delay_s * (2**attempt)
        return min(delay, self.retry_max_delay_s)


# ===================== Composite client (fallback chain) =====================


class CompositeClient:
    """Chain of LLMClient implementations with fallback.

    Tries each client in order until one succeeds. Reports success/failure
    via callbacks (used to wire into selector circuit breaker).
    """

    def __init__(
        self,
        clients: list[LLMClient],
        on_success=None,
        on_failure=None,
    ):
        if not clients:
            raise ValueError("CompositeClient requires at least one client")
        self.clients = clients
        self.on_success = on_success or (lambda model, latency_ms: None)
        self.on_failure = on_failure or (lambda model, error: None)

    def call_stream(
        self,
        model: str,
        thinking_level: str,
        prompt: str,
        max_tokens: int,
        tools: Optional[list] = None,
    ) -> tuple[str, dict]:
        last_error = None
        for client in self.clients:
            t0 = time.time()
            try:
                text, meta = client.call_stream(
                    model, thinking_level, prompt, max_tokens, tools
                )
                latency_ms = int((time.time() - t0) * 1000)
                try:
                    self.on_success(model, latency_ms)
                except Exception:
                    pass  # callback errors must not break the call
                return text, meta
            except Exception as e:
                last_error = e
                try:
                    self.on_failure(model, e)
                except Exception:
                    pass
                continue
        # All clients failed
        err_msg = (
            f"allall {len(self.clients)} LLM clients failed; last error: "
            f"{type(last_error).__name__}: {str(last_error)[:300]}"
            if last_error
            else "no LLM client available"
        )
        raise LLMPermanentError(err_msg) from last_error


# ===================== Default client =====================


def default_client() -> CompositeClient:
    """Construct the default CompositeClient from environment.

    Client priority order (first match wins):
    1. DSHBridgeClient if `DSH_BRIDGE_URL` is set
    2. OpenAICompatClient if `OPENAI_COMPAT_BASE` is set
    3. AnthropicCompatClient if `ANTHROPIC_COMPAT_BASE` is set
    4. StubClient (always last; raises NotImplementedError if reached)
    """
    from .llm_transports.stub import StubClient  # always last

    clients: list = []

    # Always try DSHBridgeClient (it resolves URL from env / credentials /
    # DEFAULT constant). Preserves v15.6 backward compatibility: DSH users
    # get the bridge automatically even without DSH_BRIDGE_URL env var.
    # The StubClient below provides the final fallback with a clear error.
    try:
        from .llm_transports.dsh_bridge import DSHBridgeClient

        clients.append(DSHBridgeClient())
    except Exception:
        pass

    oa_base = os.environ.get("OPENAI_COMPAT_BASE")
    if oa_base:
        from .llm_transports.openai_compat import OpenAICompatClient

        clients.append(OpenAICompatClient(base_url=oa_base))

    an_base = os.environ.get("ANTHROPIC_COMPAT_BASE")
    if an_base:
        from .llm_transports.anthropic_compat import AnthropicCompatClient

        clients.append(AnthropicCompatClient(base_url=an_base))

    clients.append(StubClient())

    # Wire selector circuit breaker callbacks (v15.6 compatibility).
    # Import lazily so the module is usable even if selector is unavailable.
    on_success, on_failure = _default_callbacks()

    return CompositeClient(
        clients=clients,
        on_success=on_success,
        on_failure=on_failure,
    )


def _default_callbacks():
    """Return (on_success, on_failure) callbacks that wire into selector.

    Falls back to no-op if selector import fails (e.g. in minimal installs).
    """
    try:
        from . import selector  # type: ignore

        def on_success(model, latency_ms):
            try:
                selector.record_success(model)
            except Exception:
                pass

        def on_failure(model, error):
            try:
                selector.record_failure(model)
            except Exception:
                pass

        return on_success, on_failure
    except Exception:
        return (lambda model, latency_ms: None), (lambda model, error: None)