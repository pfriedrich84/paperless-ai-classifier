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
# Embedding storage (sqlite-vec + metadata + FTS5)
# ---------------------------------------------------------------------------
def _upsert_fts(conn, doc: PaperlessDocument) -> None:
    """Insert or replace a document's text in the FTS5 index."""
    # FTS5 does not support INSERT OR REPLACE — delete then insert
    conn.execute("DELETE FROM doc_fts WHERE document_id = ?", (doc.id,))
    content = (doc.content or "")[: settings.embed_max_chars]
    conn.execute(
        "INSERT INTO doc_fts(document_id, title, content) VALUES (?, ?, ?)",
        (doc.id, doc.title or "", content),
    )


def store_embedding(doc: PaperlessDocument, embedding: list[float]) -> None:
    """Persist a pre-computed embedding for a document.

    Writes to ``doc_embeddings`` (sqlite-vec virtual table),
    ``doc_embedding_meta`` (metadata cache for the embeddings dashboard),
    and ``doc_fts`` (FTS5 full-text index for hybrid search).
    Does **not** call Ollama.
    """
    blob = _serialize_embedding(embedding)
    with get_conn() as conn:
        # vec0 virtual tables do not support INSERT OR REPLACE — delete then insert
        conn.execute("DELETE FROM doc_embeddings WHERE document_id = ?", (doc.id,))
        conn.execute(
            "INSERT INTO doc_embeddings(document_id, embedding) VALUES (?, ?)",
            (doc.id, blob),
        )
        conn.execute(
            """
            INSERT OR REPLACE INTO doc_embedding_meta
                (document_id, title, correspondent, doctype, storage_path,
                 created_date, indexed_at)
            VALUES (?, ?, ?, ?, ?, ?, datetime('now'))
            """,
            (
                doc.id,
                doc.title,
                doc.correspondent,
                doc.document_type,
                doc.storage_path,
                doc.created_date,
            ),
        )
        _upsert_fts(conn, doc)


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
    max_distance: float = 0.0,
) -> list[tuple[int, float]]:
    """Raw sqlite-vec KNN search.  Returns ``(doc_id, distance)`` pairs.

    When *max_distance* > 0, results whose L2 distance exceeds the threshold
    are filtered out.  This avoids feeding irrelevant context to the LLM.
    """
    blob = _serialize_embedding(embedding)
    with get_conn() as conn:
        if exclude_id is not None:
            # Over-fetch by 1 because the excluded doc may be in the top-k
            rows = conn.execute(
                """
                SELECT document_id, distance
                  FROM doc_embeddings
                 WHERE embedding MATCH ?
                   AND k = ?
                   AND document_id != ?
                 ORDER BY distance
                """,
                (blob, limit + 1, exclude_id),
            ).fetchall()
        else:
            rows = conn.execute(
                """
                SELECT document_id, distance
                  FROM doc_embeddings
                 WHERE embedding MATCH ?
                   AND k = ?
                 ORDER BY distance
                """,
                (blob, limit),
            ).fetchall()
    pairs = [(row["document_id"], row["distance"]) for row in rows]
    pairs = pairs[:limit]
    if max_distance > 0:
        pairs = [(doc_id, dist) for doc_id, dist in pairs if dist <= max_distance]
    return pairs


# ---------------------------------------------------------------------------
# FTS5 keyword search
# ---------------------------------------------------------------------------
def _fts_search(query: str, *, limit: int = 20) -> list[tuple[int, float]]:
    """Full-text search via FTS5.  Returns ``(doc_id, bm25_score)`` pairs.

    BM25 scores are negative (more negative = more relevant).  We negate
    them so higher values mean better matches, consistent with our usage.
    Returns an empty list if the FTS table is empty or the query is invalid.
    """
    if not query.strip():
        return []
    with get_conn() as conn:
        try:
            rows = conn.execute(
                """
                SELECT document_id, -rank AS score
                  FROM doc_fts
                 WHERE doc_fts MATCH ?
                 ORDER BY rank
                 LIMIT ?
                """,
                (query, limit),
            ).fetchall()
        except Exception:
            # Invalid FTS query syntax or empty table — graceful fallback
            return []
    return [(row["document_id"], row["score"]) for row in rows]


def _reciprocal_rank_fusion(
    vector_hits: list[tuple[int, float]],
    fts_hits: list[tuple[int, float]],
    *,
    vector_weight: float = 0.7,
    k: int = 60,
) -> list[tuple[int, float]]:
    """Merge vector and FTS results using Reciprocal Rank Fusion (RRF).

    Each result set is ranked independently.  The fused score for a document
    is ``vector_weight / (k + rank_vec) + (1 - vector_weight) / (k + rank_fts)``.
    Documents that appear in only one list get a penalty rank of ``k`` for
    the missing list.

    Returns ``(doc_id, rrf_score)`` sorted descending (highest score first).
    """
    # Build rank dicts (1-based)
    vec_rank = {doc_id: rank for rank, (doc_id, _) in enumerate(vector_hits, 1)}
    fts_rank = {doc_id: rank for rank, (doc_id, _) in enumerate(fts_hits, 1)}
    all_ids = set(vec_rank) | set(fts_rank)

    fts_weight = 1.0 - vector_weight
    scored: list[tuple[int, float]] = []
    for doc_id in all_ids:
        vr = vec_rank.get(doc_id, k)
        fr = fts_rank.get(doc_id, k)
        score = vector_weight / (k + vr) + fts_weight / (k + fr)
        scored.append((doc_id, score))

    scored.sort(key=lambda x: x[1], reverse=True)
    return scored


def _apply_metadata_filter(
    hits: list[tuple[int, float]],
    *,
    correspondent_id: int | None = None,
    doctype_id: int | None = None,
    date_from: str | None = None,
    date_to: str | None = None,
) -> list[tuple[int, float]]:
    """Post-filter search results by metadata from doc_embedding_meta."""
    if not any([correspondent_id, doctype_id, date_from, date_to]):
        return hits
    if not hits:
        return hits

    doc_ids = [did for did, _ in hits]
    placeholders = ",".join("?" * len(doc_ids))

    conditions = [f"document_id IN ({placeholders})"]
    params: list = list(doc_ids)

    if correspondent_id is not None:
        conditions.append("correspondent = ?")
        params.append(correspondent_id)
    if doctype_id is not None:
        conditions.append("doctype = ?")
        params.append(doctype_id)
    if date_from:
        conditions.append("created_date >= ?")
        params.append(date_from)
    if date_to:
        conditions.append("created_date <= ?")
        params.append(date_to)

    where = " AND ".join(conditions)
    with get_conn() as conn:
        rows = conn.execute(
            f"SELECT document_id FROM doc_embedding_meta WHERE {where}",
            params,
        ).fetchall()
    allowed = {r["document_id"] for r in rows}
    return [(did, score) for did, score in hits if did in allowed]


def _hybrid_search(
    embedding: list[float],
    query_text: str,
    *,
    exclude_id: int | None = None,
    limit: int = 10,
    max_distance: float = 0.0,
    vector_weight: float = 0.7,
    correspondent_id: int | None = None,
    doctype_id: int | None = None,
    date_from: str | None = None,
    date_to: str | None = None,
) -> list[tuple[int, float]]:
    """Combined vector + FTS5 search with Reciprocal Rank Fusion.

    When *vector_weight* is 1.0 this degrades to pure vector search.
    When it is 0.0 this degrades to pure keyword search.  The default
    0.7 blends both, which improves retrieval precision significantly.

    Optional metadata filters (correspondent, doctype, date range) are
    applied as a post-filter on the fused results.
    """
    # Over-fetch to compensate for exclusion / distance / metadata filtering
    fetch_limit = limit * 4

    # Short-circuit at weight endpoints to avoid RRF leaking cross-leg results
    if vector_weight >= 1.0:
        hits = _find_similar_ids(
            embedding, exclude_id=exclude_id, limit=fetch_limit, max_distance=max_distance
        )
    elif vector_weight <= 0.0:
        fts_hits = _fts_search(query_text, limit=fetch_limit)
        if exclude_id is not None:
            fts_hits = [(did, s) for did, s in fts_hits if did != exclude_id]
        hits = fts_hits
    else:
        # Vector leg
        vec_hits = _find_similar_ids(
            embedding, exclude_id=exclude_id, limit=fetch_limit, max_distance=max_distance
        )

        # FTS leg
        fts_hits = _fts_search(query_text, limit=fetch_limit)
        if exclude_id is not None:
            fts_hits = [(did, s) for did, s in fts_hits if did != exclude_id]

        if not fts_hits:
            hits = vec_hits
        else:
            hits = _reciprocal_rank_fusion(vec_hits, fts_hits, vector_weight=vector_weight)

    # Apply metadata filters
    hits = _apply_metadata_filter(
        hits,
        correspondent_id=correspondent_id,
        doctype_id=doctype_id,
        date_from=date_from,
        date_to=date_to,
    )
    return hits[:limit]


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
    hits = _find_similar_ids(
        embedding,
        exclude_id=doc.id,
        limit=limit,
        max_distance=settings.context_max_distance,
    )
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


async def find_similar_by_query_text_filtered(
    query_text: str,
    paperless: PaperlessClient,
    ollama: OllamaClient,
    limit: int | None = None,
    *,
    exclude_id: int | None = None,
    correspondent_id: int | None = None,
    doctype_id: int | None = None,
    date_from: str | None = None,
    date_to: str | None = None,
) -> list[SimilarDocument]:
    """Like :func:`find_similar_by_query_text` but with metadata filters.

    Used by MCP tools and chat to constrain search results by correspondent,
    document type, or date range.  Pass *exclude_id* to prevent a document
    from appearing in its own results.
    """
    if not query_text.strip():
        return []
    limit = limit or settings.context_max_docs

    try:
        vec = await ollama.embed(query_text[: settings.embed_max_chars])
    except Exception as exc:
        log.warning("filtered query embedding failed", error=str(exc))
        return []

    hits = _hybrid_search(
        vec,
        query_text,
        exclude_id=exclude_id,
        limit=limit,
        max_distance=settings.context_max_distance,
        vector_weight=settings.hybrid_search_weight,
        correspondent_id=correspondent_id,
        doctype_id=doctype_id,
        date_from=date_from,
        date_to=date_to,
    )
    return await _load_similar(hits, paperless)


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

    Uses hybrid search (vector + FTS5 with RRF) when the FTS index is
    populated, falling back to pure vector search otherwise.
    """
    if not query_text.strip():
        return []
    limit = limit or settings.context_max_docs

    try:
        vec = await ollama.embed(query_text[: settings.embed_max_chars])
    except Exception as exc:
        log.warning("chat query embedding failed", error=str(exc))
        return []

    hits = _hybrid_search(
        vec,
        query_text,
        limit=limit,
        max_distance=settings.context_max_distance,
        vector_weight=settings.hybrid_search_weight,
    )
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
