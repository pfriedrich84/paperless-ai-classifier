"""Initial and re-index jobs for the document embedding store."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import UTC, datetime

import structlog

from app.clients.ollama import OllamaClient
from app.clients.paperless import PaperlessClient
from app.db import get_conn
from app.pipeline.context_builder import index_document

log = structlog.get_logger(__name__)


# ---------------------------------------------------------------------------
# Reindex progress tracking
# ---------------------------------------------------------------------------
@dataclass
class ReindexProgress:
    """Module-level state for tracking a running reindex job."""

    running: bool = False
    total: int = 0
    done: int = 0
    failed: int = 0
    started_at: str | None = None
    finished_at: str | None = None
    error: str | None = None


_reindex_progress = ReindexProgress()
_reindex_task: asyncio.Task | None = None


def get_reindex_progress() -> ReindexProgress:
    """Return the current reindex progress (read-only snapshot)."""
    return _reindex_progress


def is_reindexing() -> bool:
    """Return ``True`` while a reindex task is running."""
    return _reindex_progress.running


# ---------------------------------------------------------------------------
# Indexing
# ---------------------------------------------------------------------------
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

    # Update progress tracking with total count
    _reindex_progress.total = len(new_docs)

    ollama.embed_retry_count = 0
    count = 0
    for i, doc in enumerate(new_docs, 1):
        try:
            await index_document(doc, ollama)
            count += 1
        except Exception as exc:
            _reindex_progress.failed += 1
            log.warning("failed to index document", doc_id=doc.id, error=str(exc))
        finally:
            _reindex_progress.done = i
            if i % 50 == 0:
                log.info("index progress", done=i, total=len(new_docs))

    log.info(
        "initial index complete",
        indexed=count,
        skipped=len(new_docs) - count,
        embed_retries=ollama.embed_retry_count,
    )
    return count


async def reindex_all(
    paperless: PaperlessClient,
    ollama: OllamaClient,
) -> int:
    """Drop all embeddings and rebuild from scratch.

    Use this when the embedding model changes.
    """
    _reindex_progress.running = True
    _reindex_progress.total = 0
    _reindex_progress.done = 0
    _reindex_progress.failed = 0
    _reindex_progress.started_at = datetime.now(tz=UTC).isoformat()
    _reindex_progress.finished_at = None
    _reindex_progress.error = None

    try:
        log.info("starting full reindex — clearing existing embeddings")
        with get_conn() as conn:
            conn.execute("DELETE FROM doc_embedding_meta")
            conn.execute("DELETE FROM doc_embeddings")

        result = await initial_index(paperless, ollama)
        _reindex_progress.finished_at = datetime.now(tz=UTC).isoformat()
        return result
    except Exception as exc:
        _reindex_progress.error = str(exc)
        raise
    finally:
        _reindex_progress.running = False


def start_reindex_task(
    paperless: PaperlessClient,
    ollama: OllamaClient,
) -> bool:
    """Launch ``reindex_all`` as a background asyncio task.

    Returns ``True`` if started, ``False`` if already running.
    """
    if _reindex_progress.running:
        return False

    async def _run() -> None:
        try:
            await reindex_all(paperless, ollama)
        except Exception as exc:
            log.error("background reindex failed", error=str(exc))

    global _reindex_task
    _reindex_task = asyncio.create_task(_run())
    return True
