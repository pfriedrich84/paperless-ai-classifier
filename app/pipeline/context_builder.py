"""Build LLM context from similar existing documents via sqlite-vec."""

from __future__ import annotations

import struct
from dataclasses import dataclass

import structlog

from app.clients.ollama import OllamaClient
from app.clients.paperless import PaperlessClient
from app.config import settings
from app.db import EMBED_DIM, get_conn
from app.models import PaperlessDocument

log = structlog.get_logger(__name__)


@dataclass
class SimilarDocument:
    """A document paired with its similarity distance from a query vector."""

    document: PaperlessDocument
    distance: float


def _serialize_embedding(vec: list[float]) -> bytes:
    """Serialize a float list to the little-endian f32 blob sqlite-vec expects."""
    return struct.pack(f"{len(vec)}f", *vec)


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
# Embedding storage (sqlite-vec + metadata)
# ---------------------------------------------------------------------------
def store_embedding(doc: PaperlessDocument, embedding: list[float]) -> None:
    """Persist a pre-computed embedding for a document.

    Writes to ``doc_embeddings`` (sqlite-vec virtual table) and
    ``doc_embedding_meta`` (metadata cache for the embeddings dashboard).
    Does **not** call Ollama.
    """
    blob = _serialize_embedding(embedding)
    with get_conn() as conn:
        conn.execute(
            "INSERT OR REPLACE INTO doc_embeddings(document_id, embedding) VALUES (?, ?)",
            (doc.id, blob),
        )
        conn.execute(
            """
            INSERT OR REPLACE INTO doc_embedding_meta
                (document_id, title, correspondent, doctype, indexed_at)
            VALUES (?, ?, ?, ?, datetime('now'))
            """,
            (doc.id, doc.title, doc.correspondent, doc.document_type),
        )


async def index_document(doc: PaperlessDocument, ollama: OllamaClient) -> None:
    """Compute + persist an embedding for a single document."""
    text = document_summary(doc)
    if not text.strip():
        return
    try:
        vec = await ollama.embed(text)
    except Exception as exc:
        log.warning("embedding failed", doc_id=doc.id, error=str(exc))
        return

    store_embedding(doc, vec)


# ---------------------------------------------------------------------------
# Similarity search (sqlite-vec KNN)
# ---------------------------------------------------------------------------
def _find_similar_ids(
    embedding: list[float],
    *,
    exclude_id: int | None = None,
    limit: int = 10,
) -> list[tuple[int, float]]:
    """Raw sqlite-vec KNN search.  Returns ``(doc_id, distance)`` pairs."""
    blob = _serialize_embedding(embedding)
    with get_conn() as conn:
        if exclude_id is not None:
            rows = conn.execute(
                """
                SELECT document_id, distance
                  FROM doc_embeddings
                 WHERE embedding MATCH ?
                   AND document_id != ?
                 ORDER BY distance
                 LIMIT ?
                """,
                (blob, exclude_id, limit),
            ).fetchall()
        else:
            rows = conn.execute(
                """
                SELECT document_id, distance
                  FROM doc_embeddings
                 WHERE embedding MATCH ?
                 ORDER BY distance
                 LIMIT ?
                """,
                (blob, limit),
            ).fetchall()
    return [(row["document_id"], row["distance"]) for row in rows]


async def _load_similar(
    hits: list[tuple[int, float]],
    paperless: PaperlessClient,
) -> list[SimilarDocument]:
    """Fetch full documents from Paperless for a list of KNN hits."""
    similar: list[SimilarDocument] = []
    for doc_id, distance in hits:
        try:
            d = await paperless.get_document(doc_id)
            similar.append(SimilarDocument(document=d, distance=distance))
        except Exception as exc:
            log.warning("failed to load similar doc", id=doc_id, error=str(exc))
    return similar


async def find_similar_with_precomputed_embedding(
    doc: PaperlessDocument,
    embedding: list[float],
    paperless: PaperlessClient,
    limit: int | None = None,
) -> list[SimilarDocument]:
    """Vector search using a pre-computed embedding vector.

    Like :func:`find_similar_documents` but skips the ``ollama.embed()``
    call — useful when the embedding has already been computed (e.g. in a
    batched pipeline that separates embedding and classification phases).
    """
    limit = limit or settings.context_max_docs
    hits = _find_similar_ids(embedding, exclude_id=doc.id, limit=limit)
    return await _load_similar(hits, paperless)


async def find_similar_with_distances(
    doc: PaperlessDocument,
    paperless: PaperlessClient,
    ollama: OllamaClient,
    limit: int | None = None,
) -> list[SimilarDocument]:
    """Return up to ``limit`` similar documents with their distance scores."""
    text = document_summary(doc)
    if not text.strip():
        return []

    try:
        vec = await ollama.embed(text)
    except Exception as exc:
        log.warning("context embedding failed", doc_id=doc.id, error=str(exc))
        return []

    return await find_similar_with_precomputed_embedding(doc, vec, paperless, limit)


async def find_similar_documents(
    doc: PaperlessDocument,
    paperless: PaperlessClient,
    ollama: OllamaClient,
    limit: int | None = None,
) -> list[PaperlessDocument]:
    """Return up to ``limit`` already-classified documents most similar to *doc*.

    Convenience wrapper around :func:`find_similar_with_distances` that strips
    distance scores for callers that only need the documents.
    """
    results = await find_similar_with_distances(doc, paperless, ollama, limit)
    return [r.document for r in results]


async def find_similar_by_query_text(
    query_text: str,
    paperless: PaperlessClient,
    ollama: OllamaClient,
    limit: int | None = None,
) -> list[SimilarDocument]:
    """Embed raw query text and find similar documents.

    Unlike :func:`find_similar_with_distances` which takes a
    :class:`PaperlessDocument`, this accepts free-form text (e.g. a user's
    chat question) and does not exclude a "source" document from results.
    """
    if not query_text.strip():
        return []
    limit = limit or settings.context_max_docs

    try:
        vec = await ollama.embed(query_text[: settings.embed_max_chars])
    except Exception as exc:
        log.warning("chat query embedding failed", error=str(exc))
        return []

    hits = _find_similar_ids(vec, limit=limit)
    return await _load_similar(hits, paperless)


def find_similar_by_id(
    document_id: int,
    limit: int = 10,
) -> list[tuple[int, float]]:
    """Vector search using a document's already-stored embedding.

    Returns ``(doc_id, distance)`` pairs.  No Ollama or Paperless API calls
    required (retrieves the stored vector from sqlite-vec).
    Returns an empty list if the document has no embedding.
    """
    with get_conn() as conn:
        row = conn.execute(
            "SELECT embedding FROM doc_embeddings WHERE document_id = ?",
            (document_id,),
        ).fetchone()
    if not row:
        return []

    vec = list(struct.unpack(f"{EMBED_DIM}f", row["embedding"]))
    return _find_similar_ids(vec, exclude_id=document_id, limit=limit)
