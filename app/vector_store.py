"""Thin abstraction over the vector + FTS storage backend.

Centralises the four core operations that would change if the project
migrates from SQLite (sqlite-vec + FTS5) to pgvector, libSQL, ChromaDB,
or any other vector store:

1. ``store``   — persist an embedding + FTS text for a document
2. ``search``  — hybrid KNN + full-text search with optional metadata filters
3. ``get``     — retrieve a stored embedding by document ID
4. ``delete_all`` — wipe all embeddings + FTS entries (for reindex)

All other code should go through this module instead of touching
``doc_embeddings`` / ``doc_fts`` tables directly.
"""

from __future__ import annotations

import struct

import structlog

from app.config import settings
from app.db import EMBED_DIM, get_conn
from app.models import PaperlessDocument

log = structlog.get_logger(__name__)


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------
def _serialize(vec: list[float]) -> bytes:
    """Serialize a float list to the little-endian f32 blob sqlite-vec expects."""
    return struct.pack(f"{len(vec)}f", *vec)


def _deserialize(blob: bytes) -> list[float]:
    """Deserialize a sqlite-vec f32 blob back to a float list."""
    return list(struct.unpack(f"{EMBED_DIM}f", blob))


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------
def store(doc: PaperlessDocument, embedding: list[float]) -> None:
    """Persist embedding, metadata, and FTS entry for *doc*."""
    blob = _serialize(embedding)
    with get_conn() as conn:
        # Vector table (vec0 does not support INSERT OR REPLACE — delete then insert)
        conn.execute("DELETE FROM doc_embeddings WHERE document_id = ?", (doc.id,))
        conn.execute(
            "INSERT INTO doc_embeddings(document_id, embedding) VALUES (?, ?)",
            (doc.id, blob),
        )
        # Metadata
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
        # FTS5
        conn.execute("DELETE FROM doc_fts WHERE document_id = ?", (doc.id,))
        content = (doc.content or "")[: settings.embed_max_chars]
        conn.execute(
            "INSERT INTO doc_fts(document_id, title, content) VALUES (?, ?, ?)",
            (doc.id, doc.title or "", content),
        )


def get(document_id: int) -> list[float] | None:
    """Retrieve the stored embedding for *document_id*, or ``None``."""
    with get_conn() as conn:
        row = conn.execute(
            "SELECT embedding FROM doc_embeddings WHERE document_id = ?",
            (document_id,),
        ).fetchone()
    if not row:
        return None
    return _deserialize(row["embedding"])


def delete_all() -> None:
    """Remove all embeddings, metadata, and FTS entries."""
    with get_conn() as conn:
        conn.execute("DELETE FROM doc_embedding_meta")
        conn.execute("DELETE FROM doc_embeddings")
        conn.execute("DELETE FROM doc_fts")
