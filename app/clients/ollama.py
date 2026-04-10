"""Ollama client: chat (with JSON mode) and embeddings."""
from __future__ import annotations

import json
from typing import Any

import httpx
import structlog

from app.config import settings

log = structlog.get_logger(__name__)


class OllamaClient:
    def __init__(self, base_url: str | None = None, model: str | None = None) -> None:
        self.base_url = (base_url or settings.ollama_url).rstrip("/")
        self.model = model or settings.ollama_model
        self.embed_model = settings.ollama_embed_model
        self._client = httpx.AsyncClient(
            base_url=self.base_url,
            timeout=httpx.Timeout(settings.ollama_timeout_seconds),
        )

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
    ) -> dict[str, Any]:
        """Call Ollama chat with format=json and parse the response."""
        payload = {
            "model": model or self.model,
            "format": "json",
            "stream": False,
            "options": {"temperature": temperature},
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
        except json.JSONDecodeError as exc:
            log.error("ollama returned invalid json", content=content[:500])
            raise ValueError(f"Invalid JSON from Ollama: {exc}") from exc

    # ---------------------------------------------------------------
    # Embeddings
    # ---------------------------------------------------------------
    async def embed(self, text: str) -> list[float]:
        payload = {"model": self.embed_model, "prompt": text}
        r = await self._client.post("/api/embeddings", json=payload)
        r.raise_for_status()
        data = r.json()
        vec = data.get("embedding")
        if not vec:
            raise ValueError("Ollama returned empty embedding")
        return vec
