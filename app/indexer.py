"""Initial and re-index jobs for the document embedding store."""

from __future__ import annotations

import structlog

from app.clients.ollama import OllamaClient
from app.clients.paperless import PaperlessClient
from app.db import get_conn
from app.pipeline.context_builder import index_document

log = structlog.get_logger(__name__)


async def initial_index(
    paperless: PaperlessClient,
    ollama: OllamaClient,
    limit: int | None = None,
) -> int:
    """Embed all already-classified documents that are not yet indexed.

    Returns the number of newly indexed documents.
    """
    log.info("starting initial embedding index", limit=limit)
    docs = await paperless.list_all_documents(limit=limit)

    # Determine which docs already have embeddings
    with get_conn() as conn:
        rows = conn.execute("SELECT document_id FROM doc_embedding_meta").fetchall()
    indexed_ids = {r["document_id"] for r in rows}

    new_docs = [d for d in docs if d.id not in indexed_ids]
    log.info(
        "documents to index", total=len(docs), already_indexed=len(indexed_ids), new=len(new_docs)
    )

    count = 0
    for i, doc in enumerate(new_docs, 1):
        try:
            await index_document(doc, ollama)
            count += 1
            if i % 50 == 0:
                log.info("index progress", done=i, total=len(new_docs))
        except Exception as exc:
            log.warning("failed to index document", doc_id=doc.id, error=str(exc))

    log.info("initial index complete", indexed=count)
    return count


async def reindex_all(
    paperless: PaperlessClient,
    ollama: OllamaClient,
) -> int:
    """Drop all embeddings and rebuild from scratch.

    Use this when the embedding model changes.
    """
    log.info("starting full reindex — clearing existing embeddings")
    with get_conn() as conn:
        conn.execute("DELETE FROM doc_embedding_meta")
        conn.execute("DELETE FROM doc_embeddings")

    return await initial_index(paperless, ollama)
