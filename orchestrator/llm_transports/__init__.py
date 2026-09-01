"""LLM transports — pluggable backends for the LLMClient protocol.

Each transport implements the `BaseLLMClient` interface and handles a specific
provider protocol:

- `stub`: raises NotImplementedError (default fallback)
- `dsh_bridge`: HTTP to the DeepSeek Harness `/api/council/llm-stream` endpoint
- `openai_compat`: any OpenAI-compatible HTTP API (DeepSeek, OpenAI, etc.)
- `anthropic_compat`: Anthropic-protocol APIs (MiniMax anthropic interface, etc.)

Add a new transport by:
1. Implementing `_do_call` on `BaseLLMClient`
2. Raising `LLMRetryableError` for transient failures (429, 5xx)
3. Raising `LLMPermanentError` for permanent failures (4xx other than 429)
4. Updating `default_client()` to include the new transport when its
   environment variable is set
"""