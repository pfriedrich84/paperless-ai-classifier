"""Build LLM context from similar existing documents via Meilisearch hybrid search."""

from __future__ import annotations

from dataclasses import dataclass

import structlog

from app.clients.meilisearch import MeiliClient
from app.clients.ollama import OllamaClient
from app.clients.paperless import PaperlessClient
from app.config import settings
from app.db import get_conn
from app.models import PaperlessDocument


@dataclass
class SimilarDocument:
    """A document paired with its similarity distance from a query vector."""

    document: PaperlessDocument
    distance: float


log = structlog.get_logger(__name__)


def document_summary(doc: PaperlessDocument) -> str:
    """Short, embedding-friendly text representation of a document.

    Limit total output to ``settings.embed_max_chars`` chars to stay within
    the embedding model's context window and avoid costly truncation retries.
    """
    parts = [doc.title or ""]
    if doc.content:
        parts.append(doc.content)
    text = "\n".join(p for p in parts if p)
    return text[: settings.embed_max_chars]


# ---------------------------------------------------------------------------
# Embedding storage
# ---------------------------------------------------------------------------
async def store_embedding(
    doc: PaperlessDocument,
    embedding: list[float],
    meili: MeiliClient,
) -> None:
    """Persist a pre-computed embedding for a document.

    Writes to Meilisearch (vector + full text for hybrid search) and to
    ``doc_embedding_meta`` (metadata cache for the embeddings dashboard).
    Does **not** call Ollama.
    """
    # Meilisearch: store document with embedding for hybrid search
    await meili.upsert_document(
        doc_id=doc.id,
        title=doc.title or "",
        content=(doc.content or "")[: settings.embed_max_chars],
        correspondent=doc.correspondent,
        document_type=doc.document_type,
        storage_path=doc.storage_path,
        tags=doc.tags,
        embedding=embedding,
    )

    # Metadata cache for embeddings dashboard listing
    with get_conn() as conn:
        conn.execute(
            """
            INSERT OR REPLACE INTO doc_embedding_meta
                (document_id, title, correspondent, doctype, indexed_at)
            VALUES (?, ?, ?, ?, datetime('now'))
            """,
            (doc.id, doc.title, doc.correspondent, doc.document_type),
        )


async def index_document(
    doc: PaperlessDocument,
    ollama: OllamaClient,
    meili: MeiliClient,
) -> None:
    """Compute + persist an embedding for a single document."""
    text = document_summary(doc)
    if not text.strip():
        return
    try:
        vec = await ollama.embed(text)
    except Exception as exc:
        log.warning("embedding failed", doc_id=doc.id, error=str(exc))
        return

    await store_embedding(doc, vec, meili)


# ---------------------------------------------------------------------------
# Similarity search
# ---------------------------------------------------------------------------
async def find_similar_with_precomputed_embedding(
    doc: PaperlessDocument,
    embedding: list[float],
    paperless: PaperlessClient,
    meili: MeiliClient,
    limit: int | None = None,
) -> list[SimilarDocument]:
    """Vector search using a pre-computed embedding vector.

    Like :func:`find_similar_with_distances` but skips the ``ollama.embed()``
    call — useful when the embedding has already been computed (e.g. in a
    batched pipeline that separates embedding and classification phases).

    Documents still in the inbox are excluded via Meilisearch filter.
    """
    limit = limit or settings.context_max_docs
    inbox_tag_id = settings.paperless_inbox_tag_id

    filter_expr = f"tags NOT IN [{inbox_tag_id}] AND id != {doc.id}"
    hits = await meili.vector_search(embedding, limit=limit, filter_expr=filter_expr)

    similar: list[SimilarDocument] = []
    for hit in hits:
        try:
            d = await paperless.get_document(hit.doc_id)
            similar.append(SimilarDocument(document=d, distance=1.0 - hit.score))
        except Exception as exc:
            log.warning("failed to load similar doc", id=hit.doc_id, error=str(exc))
    return similar


async def find_similar_with_distances(
    doc: PaperlessDocument,
    paperless: PaperlessClient,
    ollama: OllamaClient,
    meili: MeiliClient,
    limit: int | None = None,
) -> list[SimilarDocument]:
    """Return up to `limit` similar documents with their distance scores.

    Documents still in the inbox (carrying the inbox tag) are excluded — they
    have not been reviewed/approved yet and would provide unreliable context.
    """
    text = document_summary(doc)
    if not text.strip():
        return []

    try:
        vec = await ollama.embed(text)
    except Exception as exc:
        log.warning("context embedding failed", doc_id=doc.id, error=str(exc))
        return []

    return await find_similar_with_precomputed_embedding(doc, vec, paperless, meili, limit)


async def find_similar_documents(
    doc: PaperlessDocument,
    paperless: PaperlessClient,
    ollama: OllamaClient,
    meili: MeiliClient,
    limit: int | None = None,
) -> list[PaperlessDocument]:
    """Return up to `limit` already-classified documents most similar to `doc`.

    Convenience wrapper around :func:`find_similar_with_distances` that strips
    distance scores for callers that only need the documents.
    """
    results = await find_similar_with_distances(doc, paperless, ollama, meili, limit)
    return [r.document for r in results]


async def find_similar_by_query_text(
    query_text: str,
    paperless: PaperlessClient,
    ollama: OllamaClient,
    meili: MeiliClient,
    limit: int | None = None,
) -> list[SimilarDocument]:
    """Embed raw query text and find similar documents via hybrid search.

    Unlike :func:`find_similar_with_distances` which takes a
    :class:`PaperlessDocument`, this accepts free-form text (e.g. a user's
    chat question) and does not exclude a "source" document from results.

    Uses Meilisearch hybrid search (BM25 keyword + vector similarity)
    for better retrieval quality. Documents still in the inbox are excluded.
    """
    if not query_text.strip():
        return []
    limit = limit or settings.context_max_docs

    try:
        vec = await ollama.embed(query_text)
    except Exception as exc:
        log.warning("chat query embedding failed", error=str(exc))
        return []

    inbox_tag_id = settings.paperless_inbox_tag_id
    filter_expr = f"tags NOT IN [{inbox_tag_id}]"

    hits = await meili.hybrid_search(
        query_text,
        vec,
        limit=limit,
        filter_expr=filter_expr,
        hybrid_ratio=settings.meilisearch_hybrid_ratio,
    )

    similar: list[SimilarDocument] = []
    for hit in hits:
        try:
            d = await paperless.get_document(hit.doc_id)
            similar.append(SimilarDocument(document=d, distance=1.0 - hit.score))
        except Exception as exc:
            log.warning("failed to load similar doc", id=hit.doc_id, error=str(exc))
    return similar


async def find_similar_by_id(
    document_id: int,
    meili: MeiliClient,
    limit: int = 10,
) -> list[tuple[int, float]]:
    """Vector search using a document's already-stored embedding.

    Returns ``(doc_id, distance)`` pairs.  No Ollama or Paperless API calls
    required (retrieves the stored vector from Meilisearch).
    Returns an empty list if the document has no embedding.
    """
    vec = await meili.get_document_vector(document_id)
    if vec is None:
        return []

    filter_expr = f"id != {document_id}"
    hits = await meili.vector_search(vec, limit=limit, filter_expr=filter_expr)
    return [(hit.doc_id, 1.0 - hit.score) for hit in hits]
