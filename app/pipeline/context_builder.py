"""Build LLM context from similar existing documents via sqlite-vec."""

from __future__ import annotations

import struct

import structlog

from app.clients.ollama import OllamaClient
from app.clients.paperless import PaperlessClient
from app.config import settings
from app.db import EMBED_DIM, get_conn
from app.models import PaperlessDocument

log = structlog.get_logger(__name__)


def _serialize_embedding(vec: list[float]) -> bytes:
    """Serialize a float list to the little-endian f32 blob sqlite-vec expects."""
    if len(vec) != EMBED_DIM:
        raise ValueError(f"embedding dim mismatch: got {len(vec)}, expected {EMBED_DIM}")
    return struct.pack(f"{EMBED_DIM}f", *vec)


def _document_summary(doc: PaperlessDocument) -> str:
    """Short, embedding-friendly text representation of a document."""
    parts = [doc.title or ""]
    if doc.content:
        parts.append(doc.content[:2000])
    return "\n".join(p for p in parts if p)


async def index_document(doc: PaperlessDocument, ollama: OllamaClient) -> None:
    """Compute + persist an embedding for a single document."""
    text = _document_summary(doc)
    if not text.strip():
        return
    try:
        vec = await ollama.embed(text)
    except Exception as exc:
        log.warning("embedding failed", doc_id=doc.id, error=str(exc))
        return

    blob = _serialize_embedding(vec)
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


async def find_similar_documents(
    doc: PaperlessDocument,
    paperless: PaperlessClient,
    ollama: OllamaClient,
    limit: int | None = None,
) -> list[PaperlessDocument]:
    """Return up to `limit` already-classified documents most similar to `doc`.

    Documents still in the inbox (carrying the inbox tag) are excluded — they
    have not been reviewed/approved yet and would provide unreliable context.
    We overfetch from the DB to compensate for filtered-out inbox docs.
    """
    limit = limit or settings.context_max_docs
    text = _document_summary(doc)
    if not text.strip():
        return []

    try:
        vec = await ollama.embed(text)
    except Exception as exc:
        log.warning("context embedding failed", doc_id=doc.id, error=str(exc))
        return []

    blob = _serialize_embedding(vec)
    inbox_tag_id = settings.paperless_inbox_tag_id

    # Overfetch 2x to compensate for inbox docs that will be filtered out
    with get_conn() as conn:
        rows = conn.execute(
            """
            SELECT document_id
              FROM doc_embeddings
             WHERE embedding MATCH ?
               AND document_id != ?
             ORDER BY distance
             LIMIT ?
            """,
            (blob, doc.id, limit * 2),
        ).fetchall()

    if not rows:
        return []

    similar: list[PaperlessDocument] = []
    for row in rows:
        if len(similar) >= limit:
            break
        try:
            d = await paperless.get_document(row["document_id"])
            # Skip docs still in inbox — not yet reviewed/approved
            if inbox_tag_id in d.tags:
                log.debug("skipping inbox doc as context", doc_id=d.id)
                continue
            similar.append(d)
        except Exception as exc:
            log.warning("failed to load similar doc", id=row["document_id"], error=str(exc))
    return similar
