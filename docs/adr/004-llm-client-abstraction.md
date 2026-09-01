# ADR-004: LLM Client Abstraction

**Status**: Accepted (v15.6)
**Version**: v15.6+
**Supersedes**: v15.5/v15.6 L3 hardcoded DSH bridge dependency

## Context

Prior to v15.6, the Python orchestrator talked to LLMs through a hardcoded HTTP
endpoint at `http://127.0.0.1:3080/api/council/llm-stream` (DSH host-bridge
plugin). The endpoint URL was read from `config_loader.api_keys()` with a
fallback to the hardcoded constant.

This meant:
1. Running the orchestrator outside DSH was impossible (no way to call any LLM).
2. Benchmark code (`benchmark/bench/llm.py`) had to hack `sys.path` to import
   `orchestrator.stream_llm` as a top-level module.
3. Public release of the orchestrator as a standalone library would not be
   usable — anyone installing it would need to also deploy DSH.

The v15.6 redesign adds a transport abstraction layer that decouples the
orchestrator from any specific LLM endpoint.

## Decision

We introduce an `LLMClient` protocol in `orchestrator/llm_client.py` with the
following structure:

```python
class LLMClient(Protocol):
    def call_stream(
        self, model: str, thinking_level: str, prompt: str,
        max_tokens: int, tools: list | None = None,
    ) -> tuple[str, dict]: ...
```

### Architecture

```
                  orchestrator code
                  (council_v14.py / stream_llm.py)
                          │
                          ▼
              LLMClient.call_stream(...)
                          │
                          ▼
                CompositeClient
                (fallback chain)
                │
                ├─→ DSHBridgeClient      (when DSH_BRIDGE_URL set)
                ├─→ OpenAICompatClient   (when OPENAI_COMPAT_BASE set)
                ├─→ AnthropicCompatClient (when ANTHROPIC_COMPAT_BASE set)
                └─→ StubClient           (always last; clear error message)
```

### BaseLLMClient: retry + backoff + circuit breaker integration

All real transports inherit from `BaseLLMClient`, which provides:

- **Retry loop**: up to `max_retries` (default 3) attempts
- **Backoff**: exponential (`base_delay × 2^attempt`, capped at `max_delay`);
  respects `retry_after_s` from the transport's `LLMRetryableError`
- **Error classification**:
  - `LLMRetryableError` (429 / 5xx / network): retry with backoff
  - `LLMPermanentError` (4xx other than 429): fail immediately
  - Other exceptions: treated as retryable
- **Standardized meta**:
  `finish_reason / elapsed_s / usage / timeout_kind / http_status /
   retry_count / transport`

### CompositeClient: fallback chain with selector integration

`CompositeClient` chains transports in priority order. Each attempt:
- On success: calls `on_success(model, latency_ms)` (default: `selector.record_success`)
- On failure: calls `on_failure(model, error)` (default: `selector.record_failure`)

This preserves the v15.6 circuit-breaker integration (`circuit-state.json`)
without requiring changes to `selector.py`.

### `default_client()`: environment-driven selection

`default_client()` returns a `CompositeClient` with transports prepended based
on environment variables:

| Variable | Transport added | When |
|---|---|---|
| `DSH_BRIDGE_URL` | DSHBridgeClient | DSH host plugin available |
| `OPENAI_COMPAT_BASE` | OpenAICompatClient | Direct OpenAI-compat API access |
| `ANTHROPIC_COMPAT_BASE` | AnthropicCompatClient | Direct Anthropic-compat API access |
| (always) | StubClient | Final fallback; raises clear NotImplementedError |

**Default behavior**: if no env vars are set, the system has only the
`StubClient` and calls fail loudly with a clear configuration hint. This
prevents silent hangs in misconfigured environments.

### `stream_llm.py` becomes a thin compatibility wrapper

`orchestrator/stream_llm.py` is reduced to:

```python
def call_stream(model, thinking_level, prompt, max_tokens, tools=None):
    return _get_client().call_stream(model, thinking_level, prompt, max_tokens, tools)

def bridge_stream(model, thinking_level, prompt, max_tokens):
    """Backward-compat alias for call_stream."""
    return call_stream(model, thinking_level, prompt, max_tokens)
```

All existing callers (`council_v14.py`, `golden_evolve.py`, `judge_qualify.py`,
`benchmark/bench/llm.py`) continue working unchanged.

### `benchmark/bench/llm.py` no longer hacks `sys.path`

The legacy `sys.path.insert(0, ORCH_DIR)` hack is removed; `stream_llm` is
imported normally as `from orchestrator import stream_llm`. The tool-use
multi-round loop (`call_via_dsh_bridge_with_tools`) is preserved unchanged
because tool-calling is not yet abstracted (see below).

## Consequences

**Positive**:
- Public release becomes standalone-installable: clone the repo and
  `pip install -e .` works without DSH (with appropriate env vars)
- DSH users continue working transparently (default `DSH_BRIDGE_URL` keeps
  the old behavior)
- Adding new transports (e.g. local llama.cpp, Azure OpenAI) requires only a
  new transport class — no orchestrator changes
- Retry + circuit-breaker logic is centralized, not duplicated across
  transports
- Clear failure mode when no transport is configured

**Negative**:
- Tool-use multi-round loop (`benchmark/bench/llm.py:_dsh_bridge_tool_loop`)
  is not yet abstracted; it still has its own SSE parser and HTTP POST
  implementation. This is intentional — tool calling has protocol-specific
  semantics that don't fit cleanly into the simple `call_stream` protocol.
  A future ADR may extend the abstraction to cover tool loops.
- The 429 retry behavior for benchmark (30s/60s/120s exponential) is now
  duplicated: `BaseLLMClient` has its own backoff (10s base, 60s cap), and
  `call_via_dsh_bridge_with_tools` retains its 30s/60s/120s outer wrapper. This
  works correctly (innermost retry first; outer wrapper retries the whole loop
  if innermost exhausted), but is documented for future cleanup.

## Alternatives considered

- **Keep hardcoded DSH bridge**: rejected. Blocks standalone release; couples
  Python to DSH runtime.
- **Just use environment-driven URL (no abstraction)**: rejected. Doesn't
  solve multi-transport fallback or retry/circuit-breaker reuse.
- **Make tool-calling part of the protocol now**: rejected. Adds complexity
  for marginal benefit; current tool-loop code is stable and benchmark-specific.

## Implementation notes

- All public functions in `orchestrator/llm_client.py` are re-exported
  through `orchestrator.stream_llm` for backward compatibility.
- `selector.record_success` / `selector.record_failure` are imported lazily
  in `_default_callbacks` so the orchestrator can be imported even if
  `selector` is unavailable (e.g. minimal installs).
- `LLMClient` is `@runtime_checkable` so `isinstance(client, LLMClient)`
  works for protocol compliance checks.