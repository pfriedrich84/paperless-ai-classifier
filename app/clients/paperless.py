"""Paperless-NGX REST API client."""

from __future__ import annotations

import contextlib
from typing import Any

import httpx
import structlog

from app.config import settings
from app.models import PaperlessDocument, PaperlessEntity

log = structlog.get_logger(__name__)


class PaperlessClient:
    def __init__(self, base_url: str | None = None, token: str | None = None) -> None:
        self.base_url = (base_url or settings.paperless_url).rstrip("/")
        self.token = token or settings.paperless_token
        self._client = httpx.AsyncClient(
            base_url=f"{self.base_url}/api",
            headers={
                "Authorization": f"Token {self.token}",
                "Accept": "application/json; version=5",
            },
            timeout=60.0,
        )

    async def aclose(self) -> None:
        await self._client.aclose()

    # ---------------------------------------------------------------
    # Health
    # ---------------------------------------------------------------
    async def ping(self) -> bool:
        try:
            r = await self._client.get("/ui_settings/")
            return r.status_code < 500
        except Exception as exc:
            log.warning("paperless ping failed", error=str(exc))
            return False

    # ---------------------------------------------------------------
    # Documents
    # ---------------------------------------------------------------
    async def list_inbox_documents(self, inbox_tag_id: int) -> list[PaperlessDocument]:
        """Return all documents tagged with the inbox tag, paginated."""
        docs: list[PaperlessDocument] = []
        url = f"/documents/?tags__id__all={inbox_tag_id}&page_size=50"
        while url:
            r = await self._client.get(url)
            r.raise_for_status()
            data = r.json()
            for item in data.get("results", []):
                try:
                    docs.append(PaperlessDocument.model_validate(item))
                except Exception as exc:
                    log.warning("failed to parse document", error=str(exc), id=item.get("id"))
            next_url = data.get("next")
            url = self._relative(next_url) if next_url else None
        log.info("fetched inbox documents", count=len(docs))
        return docs

    async def get_document(self, document_id: int) -> PaperlessDocument:
        r = await self._client.get(f"/documents/{document_id}/")
        r.raise_for_status()
        return PaperlessDocument.model_validate(r.json())

    async def patch_document(self, document_id: int, fields: dict[str, Any]) -> None:
        """Apply metadata changes to a document."""
        if not fields:
            return
        r = await self._client.patch(f"/documents/{document_id}/", json=fields)
        r.raise_for_status()
        log.info("document patched", id=document_id, fields=list(fields.keys()))

    async def search_documents(
        self,
        query: str | None = None,
        tags: list[str] | None = None,
        correspondent: str | None = None,
        document_type: str | None = None,
        page_size: int = 25,
    ) -> list[PaperlessDocument]:
        """Full-text search with optional filters."""
        params = [f"page_size={page_size}"]
        if query:
            params.append(f"query={query}")
        if correspondent:
            params.append(f"correspondent__name__icontains={correspondent}")
        if document_type:
            params.append(f"document_type__name__icontains={document_type}")
        for tag in tags or []:
            params.append(f"tags__name__icontains={tag}")
        url = f"/documents/?{'&'.join(params)}"
        r = await self._client.get(url)
        r.raise_for_status()
        data = r.json()
        docs: list[PaperlessDocument] = []
        for item in data.get("results", []):
            with contextlib.suppress(Exception):
                docs.append(PaperlessDocument.model_validate(item))
        log.info("search_documents", count=len(docs), query=query)
        return docs

    async def list_all_documents(
        self, page_size: int = 100, limit: int | None = None
    ) -> list[PaperlessDocument]:
        """For initial embedding index."""
        docs: list[PaperlessDocument] = []
        url = f"/documents/?page_size={page_size}"
        while url:
            r = await self._client.get(url)
            r.raise_for_status()
            data = r.json()
            for item in data.get("results", []):
                with contextlib.suppress(Exception):
                    docs.append(PaperlessDocument.model_validate(item))
            if limit and len(docs) >= limit:
                return docs[:limit]
            next_url = data.get("next")
            url = self._relative(next_url) if next_url else None
        return docs

    # ---------------------------------------------------------------
    # Entities
    # ---------------------------------------------------------------
    async def list_correspondents(self) -> list[PaperlessEntity]:
        return await self._list_entity("/correspondents/")

    async def list_document_types(self) -> list[PaperlessEntity]:
        return await self._list_entity("/document_types/")

    async def list_tags(self) -> list[PaperlessEntity]:
        return await self._list_entity("/tags/")

    async def list_storage_paths(self) -> list[PaperlessEntity]:
        return await self._list_entity("/storage_paths/")

    async def create_tag(self, name: str) -> PaperlessEntity:
        r = await self._client.post("/tags/", json={"name": name})
        r.raise_for_status()
        return PaperlessEntity.model_validate(r.json())

    async def _list_entity(self, path: str) -> list[PaperlessEntity]:
        out: list[PaperlessEntity] = []
        url = f"{path}?page_size=100"
        while url:
            r = await self._client.get(url)
            r.raise_for_status()
            data = r.json()
            for item in data.get("results", []):
                with contextlib.suppress(Exception):
                    out.append(PaperlessEntity.model_validate(item))
            next_url = data.get("next")
            url = self._relative(next_url) if next_url else None
        return out

    # ---------------------------------------------------------------
    # Internal
    # ---------------------------------------------------------------
    def _relative(self, absolute_url: str) -> str:
        """Convert an absolute next-page URL to a relative path."""
        marker = "/api"
        idx = absolute_url.find(marker)
        if idx == -1:
            return absolute_url
        return absolute_url[idx + len(marker) :]
