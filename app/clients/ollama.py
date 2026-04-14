"""Ollama client: chat (with JSON mode) and embeddings."""

from __future__ import annotations

import asyncio
import json
import random
import re
from typing import Any

import httpx
import structlog

from app.config import settings

log = structlog.get_logger(__name__)

_MD_JSON_RE = re.compile(r"^\s*```(?:json)?\s*\n?(.*?)\n?\s*```\s*$", re.DOTALL)


def _strip_markdown_fences(text: str) -> str:
    """Remove markdown code fences wrapping JSON, if present."""
    m = _MD_JSON_RE.match(text)
    return m.group(1).strip() if m else text


class OllamaClient:
    def __init__(self, base_url: str | None = None, model: str | None = None) -> None:
        self.base_url = (base_url or settings.ollama_url).rstrip("/")
        self.model = model or settings.ollama_model
        self.embed_model = settings.ollama_embed_model
        self.ocr_model = settings.ollama_ocr_model
        self._client = httpx.AsyncClient(
            base_url=self.base_url,
            timeout=httpx.Timeout(settings.ollama_timeout_seconds),
        )
        self.embed_retry_count: int = 0

    async def aclose(self) -> None:
        await self._client.aclose()

    # ---------------------------------------------------------------
    # Health
    # ---------------------------------------------------------------
    async def ping(self) -> bool:
        try:
            r = await self._client.get("/api/tags")
            return r.status_code == 200
        except Exception as exc:
            log.warning("ollama ping failed", error=str(exc))
            return False

    async def unload_model(self, model: str) -> None:
        """Unload a model from VRAM via keep_alive=0."""
        try:
            await self._client.post(
                "/api/generate",
                json={"model": model, "keep_alive": 0},
            )
            log.info("model unloaded", model=model)
        except Exception as exc:
            log.warning("failed to unload model", model=model, error=str(exc))

    async def model_available(self, name: str) -> bool:
        try:
            r = await self._client.get("/api/tags")
            r.raise_for_status()
            data = r.json()
            tags = [m.get("name", "") for m in data.get("models", [])]
            return any(t == name or t.startswith(name + ":") for t in tags)
        except Exception:
            return False

    # ---------------------------------------------------------------
    # Chat (JSON mode)
    # ---------------------------------------------------------------
    async def chat_json(
        self,
        system: str,
        user: str,
        *,
        model: str | None = None,
        temperature: float = 0.1,
        num_ctx: int | None = None,
    ) -> dict[str, Any]:
        """Call Ollama chat with format=json and parse the response."""
        payload = {
            "model": model or self.model,
            "format": "json",
            "stream": False,
            "options": {
                "temperature": temperature,
                "num_ctx": num_ctx if num_ctx is not None else settings.ollama_num_ctx,
            },
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
        }
        r = await self._client.post("/api/chat", json=payload)
        r.raise_for_status()
        data = r.json()
        content = data.get("message", {}).get("content", "")
        if not content:
            raise ValueError("Ollama returned empty content")
        try:
            return json.loads(content)
        except json.JSONDecodeError:
            # Some models wrap JSON in markdown fences despite format="json"
            stripped = _strip_markdown_fences(content)
            if stripped != content:
                try:
                    return json.loads(stripped)
                except json.JSONDecodeError:
                    pass
            log.error("ollama returned invalid json", content=content[:500])
            raise ValueError(f"Invalid JSON from Ollama: {content[:200]}") from None

    # ---------------------------------------------------------------
    # Chat with vision (JSON mode)
    # ---------------------------------------------------------------
    async def chat_vision_json(
        self,
        system: str,
        user: str,
        images: list[str],
        *,
        model: str | None = None,
        temperature: float = 0.1,
        num_ctx: int | None = None,
    ) -> dict[str, Any]:
        """Call Ollama chat with images and format=json, then parse the response.

        *images* must be a list of base64-encoded image strings (no data URI prefix).
        """
        payload = {
            "model": model or self.model,
            "format": "json",
            "stream": False,
            "options": {
                "temperature": temperature,
                "num_ctx": num_ctx if num_ctx is not None else settings.ollama_num_ctx,
            },
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user, "images": images},
            ],
        }
        r = await self._client.post("/api/chat", json=payload)
        r.raise_for_status()
        data = r.json()
        content = data.get("message", {}).get("content", "")
        if not content:
            raise ValueError("Ollama returned empty content")
        try:
            return json.loads(content)
        except json.JSONDecodeError:
            stripped = _strip_markdown_fences(content)
            if stripped != content:
                try:
                    return json.loads(stripped)
                except json.JSONDecodeError:
                    pass
            log.error("ollama vision returned invalid json", content=content[:500])
            raise ValueError(f"Invalid JSON from Ollama vision: {content[:200]}") from None

    # ---------------------------------------------------------------
    # Chat (plain text, for conversational RAG)
    # ---------------------------------------------------------------
    async def chat(
        self,
        messages: list[dict[str, str]],
        *,
        model: str | None = None,
        temperature: float = 0.3,
    ) -> str:
        """Call Ollama chat and return the plain-text response.

        Unlike ``chat_json()``, this does **not** set ``format="json"`` and
        returns the raw assistant message content.  Designed for conversational
        RAG where the response is natural language.

        *messages* is the full conversation: system, prior turns, and the
        current user message.
        """
        payload = {
            "model": model or self.model,
            "stream": False,
            "options": {"temperature": temperature, "num_ctx": settings.ollama_num_ctx},
            "messages": messages,
        }
        r = await self._client.post("/api/chat", json=payload)
        r.raise_for_status()
        data = r.json()
        content = data.get("message", {}).get("content", "")
        if not content:
            raise ValueError("Ollama returned empty content")
        return content

    # ---------------------------------------------------------------
    # Embeddings
    # ---------------------------------------------------------------
    @staticmethod
    def _is_context_length_error(response: httpx.Response) -> bool:
        """Check if a 500 response is caused by input exceeding the context length."""
        try:
            body = response.text
            return "context length" in body.lower()
        except Exception:
            return False

    @staticmethod
    def _is_retryable(exc: Exception) -> bool:
        if isinstance(exc, httpx.HTTPStatusError):
            code = exc.response.status_code
            return code == 429 or code >= 500
        return isinstance(exc, (httpx.ConnectError, httpx.ReadTimeout))

    async def embed(self, text: str) -> list[float]:
        max_retries = settings.ollama_embed_retries
        base_delay = settings.ollama_embed_retry_base_delay
        prompt = text
        last_exc: Exception | None = None

        for attempt in range(1 + max_retries):
            payload = {
                "model": self.embed_model,
                "prompt": prompt,
                "options": {"num_ctx": settings.ollama_embed_num_ctx},
            }
            try:
                r = await self._client.post("/api/embeddings", json=payload)
                r.raise_for_status()
                data = r.json()
                vec = data.get("embedding")
                if not vec:
                    raise ValueError("Ollama returned empty embedding")
                return vec
            except httpx.HTTPStatusError as exc:
                last_exc = exc
                if attempt < max_retries and self._is_context_length_error(exc.response):
                    # Input too long — truncate by 50% and retry immediately
                    prompt = prompt[: int(len(prompt) * 0.50)]
                    self.embed_retry_count += 1
                    log.warning(
                        "embedding input exceeds context length, truncating"
                        " — consider lowering EMBED_MAX_CHARS"
                        f" (currently {settings.embed_max_chars})",
                        attempt=attempt + 1,
                        new_len=len(prompt),
                    )
                    continue
                if attempt < max_retries and self._is_retryable(exc):
                    delay = base_delay * (2**attempt) + random.uniform(0, 0.5)
                    self.embed_retry_count += 1
                    log.warning(
                        "embedding request failed, retrying",
                        attempt=attempt + 1,
                        delay_s=round(delay, 2),
                        status=exc.response.status_code,
                    )
                    await asyncio.sleep(delay)
                    continue
                raise
            except Exception as exc:
                last_exc = exc
                if attempt < max_retries and self._is_retryable(exc):
                    delay = base_delay * (2**attempt) + random.uniform(0, 0.5)
                    self.embed_retry_count += 1
                    log.warning(
                        "embedding request failed, retrying",
                        attempt=attempt + 1,
                        delay_s=round(delay, 2),
                        error=str(exc),
                    )
                    await asyncio.sleep(delay)
                    continue
                raise

        raise last_exc  # type: ignore[misc]
