"""Meilisearch client: hybrid search (BM25 + vector similarity)."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import structlog

from app.config import settings

log = structlog.get_logger(__name__)

INDEX_UID = "documents"


@dataclass
class MeiliHit:
    """A single search result from Meilisearch."""

    doc_id: int
    score: float


class MeiliClient:
    """Async Meilisearch client for hybrid document search."""

    def __init__(
        self,
        url: str | None = None,
        api_key: str | None = None,
    ) -> None:
        from meilisearch_python_sdk import AsyncClient

        self._url = (url or settings.meilisearch_url).rstrip("/")
        self._api_key = api_key or settings.meilisearch_api_key or None
        self._client = AsyncClient(self._url, self._api_key, timeout=30)
        self._index = self._client.index(INDEX_UID)

    async def aclose(self) -> None:
        await self._client.aclose()

    # ---------------------------------------------------------------
    # Health
    # ---------------------------------------------------------------
    async def ping(self) -> bool:
        try:
            await self._client.health()
            return True
        except Exception as exc:
            log.warning("meilisearch ping failed", error=str(exc))
            return False

    # ---------------------------------------------------------------
    # Index setup (idempotent)
    # ---------------------------------------------------------------
    async def ensure_index(self, embed_dim: int) -> None:
        """Create or update the documents index with vector + search config."""
        from meilisearch_python_sdk.models.settings import (
            Embedders,
            MeilisearchSettings,
            UserProvidedEmbedder,
        )

        index_settings = MeilisearchSettings(
            searchable_attributes=["title", "content"],
            filterable_attributes=["tags", "correspondent", "document_type", "storage_path", "id"],
            embedders=Embedders(
                embedders={
                    "ollama": UserProvidedEmbedder(
                        source="userProvided",
                        dimensions=embed_dim,
                    ),
                }
            ),
        )

        try:
            # create_index is idempotent when wait=True
            await self._client.create_index(
                INDEX_UID,
                primary_key="id",
                settings=index_settings,
                wait=True,
            )
            log.info("meilisearch index ensured", uid=INDEX_UID, embed_dim=embed_dim)
        except Exception:
            # Index may already exist — update settings instead
            try:
                await self._index.update_settings(index_settings)
                log.info("meilisearch index settings updated", uid=INDEX_UID)
            except Exception as exc:
                log.error("meilisearch index setup failed", error=str(exc))
                raise

        # Refresh index reference
        self._index = self._client.index(INDEX_UID)

    # ---------------------------------------------------------------
    # Document storage
    # ---------------------------------------------------------------
    async def upsert_document(
        self,
        doc_id: int,
        title: str,
        content: str,
        correspondent: int | None,
        document_type: int | None,
        storage_path: int | None,
        tags: list[int],
        embedding: list[float],
    ) -> None:
        """Add or update a single document with its embedding vector."""
        doc: dict[str, Any] = {
            "id": doc_id,
            "title": title,
            "content": content,
            "correspondent": correspondent,
            "document_type": document_type,
            "storage_path": storage_path,
            "tags": tags,
            "_vectors": {"ollama": {"embeddings": embedding, "regenerate": False}},
        }
        await self._index.update_documents([doc])
        log.debug("meilisearch document upserted", doc_id=doc_id)

    async def upsert_documents_batch(
        self,
        docs: list[dict[str, Any]],
    ) -> None:
        """Batch upsert documents (each dict must have 'id' and '_vectors')."""
        if not docs:
            return
        await self._index.update_documents(docs)
        log.info("meilisearch batch upserted", count=len(docs))

    async def delete_all_documents(self) -> None:
        """Remove all documents from the index."""
        await self._index.delete_all_documents()
        log.info("meilisearch all documents deleted")

    # ---------------------------------------------------------------
    # Search
    # ---------------------------------------------------------------
    async def hybrid_search(
        self,
        query_text: str,
        query_embedding: list[float],
        *,
        limit: int = 10,
        filter_expr: str | None = None,
        hybrid_ratio: float | None = None,
    ) -> list[MeiliHit]:
        """Combined BM25 keyword + vector similarity search."""
        from meilisearch_python_sdk.models.search import Hybrid

        ratio = hybrid_ratio if hybrid_ratio is not None else settings.meilisearch_hybrid_ratio
        result = await self._index.search(
            query_text,
            limit=limit,
            filter=filter_expr,
            vector=query_embedding,
            hybrid=Hybrid(semantic_ratio=ratio, embedder="ollama"),
            show_ranking_score=True,
            attributes_to_retrieve=["id"],
        )

        return [
            MeiliHit(
                doc_id=hit["id"],
                score=hit.get("_rankingScore", 0.0),
            )
            for hit in result.hits
        ]

    async def vector_search(
        self,
        query_embedding: list[float],
        *,
        limit: int = 10,
        filter_expr: str | None = None,
    ) -> list[MeiliHit]:
        """Pure vector similarity search (semantic_ratio=1.0)."""
        return await self.hybrid_search(
            "",
            query_embedding,
            limit=limit,
            filter_expr=filter_expr,
            hybrid_ratio=1.0,
        )

    async def get_document_vector(self, doc_id: int) -> list[float] | None:
        """Retrieve the stored embedding vector for a document."""
        try:
            doc = await self._index.get_document(
                str(doc_id),
                fields=["id"],
                retrieve_vectors=True,
            )
            vectors = doc.get("_vectors", {})
            ollama_vec = vectors.get("ollama", {})
            embeddings = ollama_vec.get("embeddings")
            if isinstance(embeddings, list) and embeddings:
                # Meilisearch may return nested list [[...]] or flat [...]
                if isinstance(embeddings[0], list):
                    return embeddings[0]
                return embeddings
        except Exception as exc:
            log.debug("meilisearch get_document_vector failed", doc_id=doc_id, error=str(exc))
        return None

    # ---------------------------------------------------------------
    # Stats
    # ---------------------------------------------------------------
    async def get_stats(self) -> dict[str, Any]:
        """Return index statistics."""
        try:
            stats = await self._index.get_stats()
            return {
                "number_of_documents": stats.number_of_documents,
                "is_indexing": stats.is_indexing,
            }
        except Exception as exc:
            log.warning("meilisearch stats failed", error=str(exc))
            return {"number_of_documents": 0, "is_indexing": False}
