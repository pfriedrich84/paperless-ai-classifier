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


@dataclass
class SimilarDocument:
    """A document paired with its similarity distance from a query vector."""

    document: PaperlessDocument
    distance: float


log = structlog.get_logger(__name__)


def _serialize_embedding(vec: list[float]) -> bytes:
    """Serialize a float list to the little-endian f32 blob sqlite-vec expects."""
    if len(vec) != EMBED_DIM:
        raise ValueError(f"embedding dim mismatch: got {len(vec)}, expected {EMBED_DIM}")
    return struct.pack(f"{EMBED_DIM}f", *vec)


def document_summary(doc: PaperlessDocument) -> str:
    """Short, embedding-friendly text representation of a document.

    Limit content to ``settings.embed_max_chars`` chars to stay within the
    embedding model's context window and avoid costly truncation retries.
    """
    parts = [doc.title or ""]
    if doc.content:
        parts.append(doc.content[: settings.embed_max_chars])
    return "\n".join(p for p in parts if p)


# ---------------------------------------------------------------------------
# Embedding storage (no Ollama call needed)
# ---------------------------------------------------------------------------
def store_embedding(doc: PaperlessDocument, embedding: list[float]) -> None:
    """Persist a pre-computed embedding for a document.

    Writes to both ``doc_embeddings`` (vector) and ``doc_embedding_meta``
    (metadata for cache invalidation).  Does **not** call Ollama.
    """
    blob = _serialize_embedding(embedding)
    with get_conn() as conn:
        # sqlite-vec vec0 tables don't support INSERT OR REPLACE,
        # so delete any existing row first.
        conn.execute("DELETE FROM doc_embeddings WHERE document_id = ?", (doc.id,))
        conn.execute(
            "INSERT INTO doc_embeddings(document_id, embedding) VALUES (?, ?)",
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
# Similarity search
# ---------------------------------------------------------------------------
async def find_similar_with_precomputed_embedding(
    doc: PaperlessDocument,
    embedding: list[float],
    paperless: PaperlessClient,
    limit: int | None = None,
) -> list[SimilarDocument]:
    """KNN search using a pre-computed embedding vector.

    Like :func:`find_similar_with_distances` but skips the ``ollama.embed()``
    call — useful when the embedding has already been computed (e.g. in a
    batched pipeline that separates embedding and classification phases).

    Documents still in the inbox are excluded (same filtering as
    :func:`find_similar_with_distances`).
    """
    limit = limit or settings.context_max_docs
    blob = _serialize_embedding(embedding)
    inbox_tag_id = settings.paperless_inbox_tag_id

    # Overfetch to compensate for inbox docs + self that will be filtered out.
    # sqlite-vec vec0 requires `k = ?` in WHERE for KNN queries;
    # LIMIT alone is insufficient when other constraints are present.
    k = limit * 2 + 1
    with get_conn() as conn:
        rows = conn.execute(
            """
            SELECT document_id, distance
              FROM doc_embeddings
             WHERE embedding MATCH ?
               AND k = ?
             ORDER BY distance
            """,
            (blob, k),
        ).fetchall()

    # Exclude the source document (vec0 KNN cannot filter by document_id)
    rows = [r for r in rows if r["document_id"] != doc.id]

    if not rows:
        return []

    similar: list[SimilarDocument] = []
    for row in rows:
        if len(similar) >= limit:
            break
        try:
            d = await paperless.get_document(row["document_id"])
            # Skip docs still in inbox — not yet reviewed/approved
            if inbox_tag_id in d.tags:
                log.debug("skipping inbox doc as context", doc_id=d.id)
                continue
            similar.append(SimilarDocument(document=d, distance=row["distance"]))
        except Exception as exc:
            log.warning("failed to load similar doc", id=row["document_id"], error=str(exc))
    return similar


async def find_similar_with_distances(
    doc: PaperlessDocument,
    paperless: PaperlessClient,
    ollama: OllamaClient,
    limit: int | None = None,
) -> list[SimilarDocument]:
    """Return up to `limit` similar documents with their distance scores.

    Documents still in the inbox (carrying the inbox tag) are excluded — they
    have not been reviewed/approved yet and would provide unreliable context.
    We overfetch from the DB to compensate for filtered-out inbox docs.
    """
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
    """Return up to `limit` already-classified documents most similar to `doc`.

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
    """Embed raw query text and find similar documents via KNN.

    Unlike :func:`find_similar_with_distances` which takes a
    :class:`PaperlessDocument`, this accepts free-form text (e.g. a user's
    chat question) and does not exclude a "source" document from results.

    Documents still in the inbox are excluded (same filtering as other
    similarity functions).
    """
    if not query_text.strip():
        return []
    limit = limit or settings.context_max_docs

    try:
        vec = await ollama.embed(query_text)
    except Exception as exc:
        log.warning("chat query embedding failed", error=str(exc))
        return []

    blob = _serialize_embedding(vec)
    inbox_tag_id = settings.paperless_inbox_tag_id

    k = limit * 2
    with get_conn() as conn:
        rows = conn.execute(
            """
            SELECT document_id, distance
              FROM doc_embeddings
             WHERE embedding MATCH ?
               AND k = ?
             ORDER BY distance
            """,
            (blob, k),
        ).fetchall()

    if not rows:
        return []

    similar: list[SimilarDocument] = []
    for row in rows:
        if len(similar) >= limit:
            break
        try:
            d = await paperless.get_document(row["document_id"])
            if inbox_tag_id in d.tags:
                continue
            similar.append(SimilarDocument(document=d, distance=row["distance"]))
        except Exception as exc:
            log.warning("failed to load similar doc", id=row["document_id"], error=str(exc))
    return similar


def find_similar_by_id(
    document_id: int,
    limit: int = 10,
) -> list[tuple[int, float]]:
    """KNN search using a document's already-stored embedding.

    Returns ``(doc_id, distance)`` pairs.  Purely local — no Ollama or
    Paperless API calls required.  Returns an empty list if the document
    has no embedding.
    """
    with get_conn() as conn:
        embedding_row = conn.execute(
            "SELECT embedding FROM doc_embeddings WHERE document_id = ?",
            (document_id,),
        ).fetchone()
        if embedding_row is None:
            return []

        blob = embedding_row["embedding"]
        k = limit + 1  # +1 to account for the source doc itself
        rows = conn.execute(
            """
            SELECT document_id, distance
              FROM doc_embeddings
             WHERE embedding MATCH ?
               AND k = ?
             ORDER BY distance
            """,
            (blob, k),
        ).fetchall()

    return [(r["document_id"], r["distance"]) for r in rows if r["document_id"] != document_id][
        :limit
    ]
