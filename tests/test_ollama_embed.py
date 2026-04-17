"""Tests for OllamaClient.embed() retry/truncation and chat_json() parsing."""

from __future__ import annotations

import json
from unittest.mock import AsyncMock, patch

import httpx
import pytest

from app.clients.ollama import OllamaClient
from app.db import EMBED_DIM


def _make_response(
    status_code: int, json_body: dict | None = None, text: str = ""
) -> httpx.Response:
    """Build a minimal httpx.Response for testing."""
    r = httpx.Response(
        status_code=status_code,
        json=json_body,
        request=httpx.Request("POST", "http://test/api/embeddings"),
    )
    if text:
        r._content = text.encode()
    return r


@pytest.fixture()
def client() -> OllamaClient:
    c = OllamaClient.__new__(OllamaClient)
    c.base_url = "http://test:11434"
    c.model = "test-model"
    c.embed_model = "test-embed"
    c._client = AsyncMock()
    c.embed_retry_count = 0
    return c


async def test_embed_succeeds_without_retry(client: OllamaClient):
    """Successful embed on first attempt — no retries needed."""
    embedding = [0.1] * EMBED_DIM
    client._client.post = AsyncMock(return_value=_make_response(200, {"embedding": embedding}))

    result = await client.embed("hello world")

    assert result == embedding
    assert client.embed_retry_count == 0
    assert client._client.post.call_count == 1


async def test_embed_retries_on_transient_500_then_succeeds(client: OllamaClient):
    """Transient 500 (not context-length) triggers backoff retry."""
    embedding = [0.1] * EMBED_DIM
    client._client.post = AsyncMock(
        side_effect=[
            _make_response(500, text='{"error": "internal error"}'),
            _make_response(200, {"embedding": embedding}),
        ]
    )

    with patch("app.clients.ollama.asyncio.sleep", new_callable=AsyncMock) as mock_sleep:
        result = await client.embed("hello world")

    assert result == embedding
    assert client.embed_retry_count == 1
    assert client._client.post.call_count == 2
    mock_sleep.assert_called_once()


async def test_embed_retries_exhausted_raises(client: OllamaClient):
    """All retries exhausted — raises the last HTTPStatusError."""
    client._client.post = AsyncMock(
        return_value=_make_response(500, text='{"error": "internal error"}')
    )

    with (
        patch("app.clients.ollama.asyncio.sleep", new_callable=AsyncMock),
        patch("app.clients.ollama.settings") as mock_settings,
    ):
        mock_settings.ollama_embed_retries = 2
        mock_settings.ollama_embed_retry_base_delay = 0.01
        with pytest.raises(httpx.HTTPStatusError):
            await client.embed("hello world")

    # 1 initial + 2 retries = 3 attempts, 2 retries counted
    assert client.embed_retry_count == 2
    assert client._client.post.call_count == 3


async def test_embed_no_retry_on_4xx(client: OllamaClient):
    """4xx client error (not 429) raises immediately without retry."""
    client._client.post = AsyncMock(
        return_value=_make_response(400, text='{"error": "bad request"}')
    )

    with pytest.raises(httpx.HTTPStatusError):
        await client.embed("hello world")

    assert client.embed_retry_count == 0
    assert client._client.post.call_count == 1


async def test_embed_retries_on_429(client: OllamaClient):
    """429 rate limit triggers retry."""
    embedding = [0.1] * EMBED_DIM
    client._client.post = AsyncMock(
        side_effect=[
            _make_response(429, text="rate limited"),
            _make_response(200, {"embedding": embedding}),
        ]
    )

    with patch("app.clients.ollama.asyncio.sleep", new_callable=AsyncMock):
        result = await client.embed("hello world")

    assert result == embedding
    assert client.embed_retry_count == 1


async def test_embed_retries_on_connect_error(client: OllamaClient):
    """ConnectError triggers retry."""
    embedding = [0.1] * EMBED_DIM
    client._client.post = AsyncMock(
        side_effect=[
            httpx.ConnectError("connection refused"),
            _make_response(200, {"embedding": embedding}),
        ]
    )

    with patch("app.clients.ollama.asyncio.sleep", new_callable=AsyncMock):
        result = await client.embed("hello world")

    assert result == embedding
    assert client.embed_retry_count == 1


async def test_embed_context_length_error_truncates_and_retries(client: OllamaClient):
    """Context-length 500 triggers text truncation (no backoff) and retries."""
    long_text = "a" * 2000
    embedding = [0.1] * EMBED_DIM

    client._client.post = AsyncMock(
        side_effect=[
            _make_response(500, text='{"error": "the input length exceeds the context length"}'),
            _make_response(200, {"embedding": embedding}),
        ]
    )

    result = await client.embed(long_text)

    assert result == embedding
    assert client.embed_retry_count == 1
    # Second call should have truncated prompt (50% of original)
    second_call_payload = client._client.post.call_args_list[1][1]["json"]
    assert len(second_call_payload["prompt"]) == int(len(long_text) * 0.50)


async def test_embed_context_length_progressive_truncation(client: OllamaClient):
    """Multiple context-length errors cause progressive truncation."""
    long_text = "a" * 2000
    embedding = [0.1] * EMBED_DIM

    client._client.post = AsyncMock(
        side_effect=[
            _make_response(500, text='{"error": "the input length exceeds the context length"}'),
            _make_response(500, text='{"error": "the input length exceeds the context length"}'),
            _make_response(200, {"embedding": embedding}),
        ]
    )

    result = await client.embed(long_text)

    assert result == embedding
    assert client.embed_retry_count == 2
    # Third call: 2000 * 0.50 * 0.50 = 500
    third_call_payload = client._client.post.call_args_list[2][1]["json"]
    assert len(third_call_payload["prompt"]) == int(int(2000 * 0.50) * 0.50)


async def test_embed_retry_disabled_when_zero(client: OllamaClient):
    """With retries=0, errors raise immediately."""
    client._client.post = AsyncMock(
        return_value=_make_response(500, text='{"error": "internal error"}')
    )

    with patch("app.clients.ollama.settings") as mock_settings:
        mock_settings.ollama_embed_retries = 0
        mock_settings.ollama_embed_retry_base_delay = 1.0
        with pytest.raises(httpx.HTTPStatusError):
            await client.embed("hello world")

    assert client.embed_retry_count == 0
    assert client._client.post.call_count == 1


async def test_embed_raises_on_unexpected_dimension(client: OllamaClient):
    """Embedding vectors with unexpected dimension should fail fast."""
    client._client.post = AsyncMock(return_value=_make_response(200, {"embedding": [0.1] * 10}))

    with patch("app.clients.ollama.settings") as mock_settings:
        mock_settings.ollama_embed_retries = 0
        mock_settings.ollama_embed_retry_base_delay = 0.01
        mock_settings.ollama_embed_num_ctx = 8192
        with pytest.raises(ValueError, match="Unexpected embedding dimension"):
            await client.embed("hello world")


# ---------------------------------------------------------------------------
# chat_json — markdown fence stripping + num_ctx
# ---------------------------------------------------------------------------


def _make_chat_response(content: str) -> httpx.Response:
    """Build a minimal httpx.Response mimicking an Ollama /api/chat reply."""
    body = {"message": {"role": "assistant", "content": content}}
    return httpx.Response(
        status_code=200,
        json=body,
        request=httpx.Request("POST", "http://test/api/chat"),
    )


async def test_chat_json_handles_bare_json(client: OllamaClient):
    """Clean JSON parses without issues (regression check)."""
    payload = {"title": "Test", "confidence": 90}
    client._client.post = AsyncMock(return_value=_make_chat_response(json.dumps(payload)))

    with patch("app.clients.ollama.settings") as mock_settings:
        mock_settings.ollama_num_ctx = 4096
        result = await client.chat_json(system="sys", user="usr")

    assert result == payload


async def test_chat_json_strips_markdown_fences(client: OllamaClient):
    """JSON wrapped in ```json ... ``` fences should be parsed successfully."""
    payload = {"title": "Rechnung", "confidence": 85}
    fenced = f"```json\n{json.dumps(payload)}\n```"
    client._client.post = AsyncMock(return_value=_make_chat_response(fenced))

    with patch("app.clients.ollama.settings") as mock_settings:
        mock_settings.ollama_num_ctx = 4096
        result = await client.chat_json(system="sys", user="usr")

    assert result == payload


async def test_chat_json_strips_bare_fences(client: OllamaClient):
    """JSON wrapped in ``` ... ``` (no language tag) should also parse."""
    payload = {"key": "value"}
    fenced = f"```\n{json.dumps(payload)}\n```"
    client._client.post = AsyncMock(return_value=_make_chat_response(fenced))

    with patch("app.clients.ollama.settings") as mock_settings:
        mock_settings.ollama_num_ctx = 4096
        result = await client.chat_json(system="sys", user="usr")

    assert result == payload


async def test_chat_json_strips_yaml_fence(client: OllamaClient):
    """JSON prefixed with '---' (YAML frontmatter delimiter) should parse."""
    payload = {"title": "Laborbefund", "confidence": 90}
    fenced = f"---\n{json.dumps(payload)}"
    client._client.post = AsyncMock(return_value=_make_chat_response(fenced))

    with patch("app.clients.ollama.settings") as mock_settings:
        mock_settings.ollama_num_ctx = 4096
        result = await client.chat_json(system="sys", user="usr")

    assert result == payload


async def test_chat_json_raises_on_invalid_content(client: OllamaClient):
    """Truly invalid (non-JSON, non-fenced) content raises ValueError."""
    client._client.post = AsyncMock(return_value=_make_chat_response("this is not json at all"))

    with patch("app.clients.ollama.settings") as mock_settings:
        mock_settings.ollama_num_ctx = 4096
        with pytest.raises(ValueError, match="Invalid JSON from Ollama"):
            await client.chat_json(system="sys", user="usr")


async def test_chat_json_passes_num_ctx(client: OllamaClient):
    """The payload sent to Ollama includes num_ctx in options."""
    payload = {"ok": True}
    client._client.post = AsyncMock(return_value=_make_chat_response(json.dumps(payload)))

    with patch("app.clients.ollama.settings") as mock_settings:
        mock_settings.ollama_num_ctx = 8192
        await client.chat_json(system="sys", user="usr")

    sent_payload = client._client.post.call_args[1]["json"]
    assert sent_payload["options"]["num_ctx"] == 8192


async def test_chat_json_passes_custom_num_ctx(client: OllamaClient):
    """Explicit num_ctx override takes precedence over settings default."""
    payload = {"ok": True}
    client._client.post = AsyncMock(return_value=_make_chat_response(json.dumps(payload)))

    with patch("app.clients.ollama.settings") as mock_settings:
        mock_settings.ollama_num_ctx = 8192
        await client.chat_json(system="sys", user="usr", num_ctx=131072)

    sent_payload = client._client.post.call_args[1]["json"]
    assert sent_payload["options"]["num_ctx"] == 131072


async def test_chat_json_retries_on_transient_500(client: OllamaClient):
    """chat_json retries once on transient 500 and then succeeds."""
    payload = {"title": "Recovered", "confidence": 80}
    client._client.post = AsyncMock(
        side_effect=[
            _make_response(500, text='{"error": "internal error"}'),
            _make_chat_response(json.dumps(payload)),
        ]
    )

    with (
        patch("app.clients.ollama.asyncio.sleep", new_callable=AsyncMock) as mock_sleep,
        patch("app.clients.ollama.settings") as mock_settings,
    ):
        mock_settings.ollama_num_ctx = 4096
        mock_settings.ollama_chat_retries = 1
        mock_settings.ollama_chat_retry_base_delay = 0.01
        result = await client.chat_json(system="sys", user="usr")

    assert result == payload
    assert client._client.post.call_count == 2
    mock_sleep.assert_called_once()


async def test_chat_retries_on_connect_error(client: OllamaClient):
    """Plain chat retries on transient transport errors."""
    client._client.post = AsyncMock(
        side_effect=[
            httpx.ConnectError("connection refused"),
            _make_chat_response("ok"),
        ]
    )

    with (
        patch("app.clients.ollama.asyncio.sleep", new_callable=AsyncMock) as mock_sleep,
        patch("app.clients.ollama.settings") as mock_settings,
    ):
        mock_settings.ollama_num_ctx = 4096
        mock_settings.ollama_chat_retries = 1
        mock_settings.ollama_chat_retry_base_delay = 0.01
        result = await client.chat(messages=[{"role": "user", "content": "hi"}])

    assert result == "ok"
    assert client._client.post.call_count == 2
    mock_sleep.assert_called_once()


async def test_chat_vision_json_passes_default_num_ctx(client: OllamaClient):
    """Vision chat uses settings.ollama_num_ctx when no override is given."""
    payload = {"ok": True}
    client._client.post = AsyncMock(return_value=_make_chat_response(json.dumps(payload)))

    with patch("app.clients.ollama.settings") as mock_settings:
        mock_settings.ollama_num_ctx = 8192
        await client.chat_vision_json(system="sys", user="usr", images=["abc123"])

    sent_payload = client._client.post.call_args[1]["json"]
    assert sent_payload["options"]["num_ctx"] == 8192


async def test_chat_vision_json_passes_custom_num_ctx(client: OllamaClient):
    """Explicit num_ctx override takes precedence for vision calls."""
    payload = {"ok": True}
    client._client.post = AsyncMock(return_value=_make_chat_response(json.dumps(payload)))

    with patch("app.clients.ollama.settings") as mock_settings:
        mock_settings.ollama_num_ctx = 8192
        await client.chat_vision_json(system="sys", user="usr", images=["abc123"], num_ctx=131072)

    sent_payload = client._client.post.call_args[1]["json"]
    assert sent_payload["options"]["num_ctx"] == 131072


# ---------------------------------------------------------------------------
# unload_model — model swap delay
# ---------------------------------------------------------------------------
async def test_is_retryable_covers_additional_transport_errors(client: OllamaClient):
    """Retryability includes Pool/Write timeouts and protocol/read-write errors."""
    assert client._is_retryable(httpx.PoolTimeout("pool timeout"))
    assert client._is_retryable(httpx.WriteTimeout("write timeout"))
    assert client._is_retryable(httpx.RemoteProtocolError("bad protocol"))


async def test_unload_model_sleeps_for_swap_delay(client: OllamaClient):
    """unload_model(swap=True) waits for the configured swap delay."""
    client._client.post = AsyncMock(
        return_value=httpx.Response(200, request=httpx.Request("POST", "http://test/api/generate"))
    )

    with (
        patch("app.clients.ollama.settings") as mock_settings,
        patch("app.clients.ollama.asyncio.sleep", new_callable=AsyncMock) as mock_sleep,
    ):
        mock_settings.ollama_model_swap_delay = 5.0
        await client.unload_model("test-model", swap=True)

    mock_sleep.assert_awaited_once_with(5.0)


async def test_unload_model_skips_sleep_when_zero(client: OllamaClient):
    """unload_model(swap=True) does not sleep when swap delay is 0."""
    client._client.post = AsyncMock(
        return_value=httpx.Response(200, request=httpx.Request("POST", "http://test/api/generate"))
    )

    with (
        patch("app.clients.ollama.settings") as mock_settings,
        patch("app.clients.ollama.asyncio.sleep", new_callable=AsyncMock) as mock_sleep,
    ):
        mock_settings.ollama_model_swap_delay = 0
        await client.unload_model("test-model", swap=True)

    mock_sleep.assert_not_awaited()


async def test_unload_model_no_sleep_without_swap(client: OllamaClient):
    """unload_model() without swap=True never sleeps (terminal cleanup)."""
    client._client.post = AsyncMock(
        return_value=httpx.Response(200, request=httpx.Request("POST", "http://test/api/generate"))
    )

    with (
        patch("app.clients.ollama.settings") as mock_settings,
        patch("app.clients.ollama.asyncio.sleep", new_callable=AsyncMock) as mock_sleep,
    ):
        mock_settings.ollama_model_swap_delay = 5.0
        await client.unload_model("test-model")

    mock_sleep.assert_not_awaited()
