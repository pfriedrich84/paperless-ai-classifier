"""Initial and re-index jobs for the document embedding store."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import UTC, datetime

import structlog

from app.clients.meilisearch import MeiliClient
from app.clients.ollama import OllamaClient
from app.clients.paperless import PaperlessClient
from app.config import settings
from app.db import get_conn
from app.pipeline.context_builder import index_document
from app.pipeline.ocr_correction import (
    cache_ocr_correction,
    effective_ocr_mode,
    get_cached_ocr,
    maybe_correct_ocr,
)

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
    cancelled: bool = False


_reindex_progress = ReindexProgress()
_reindex_task: asyncio.Task | None = None


def get_reindex_progress() -> ReindexProgress:
    """Return the current reindex progress (read-only snapshot)."""
    return _reindex_progress


def is_reindexing() -> bool:
    """Return ``True`` while a reindex task is running."""
    return _reindex_progress.running


def cancel_reindex() -> bool:
    """Request cancellation of the running reindex task.

    Returns ``True`` if cancellation was requested, ``False`` if not running.
    """
    if not _reindex_progress.running:
        return False
    _reindex_progress.cancelled = True
    return True


# ---------------------------------------------------------------------------
# Indexing
# ---------------------------------------------------------------------------
async def initial_index(
    paperless: PaperlessClient,
    ollama: OllamaClient,
    meili: MeiliClient,
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
        if _reindex_progress.cancelled:
            log.info("reindex cancelled by user", done=i - 1, total=len(new_docs))
            break
        try:
            # Use cached OCR-corrected text if available
            cached = get_cached_ocr(doc.id)
            if cached:
                doc = doc.model_copy(update={"content": cached})
            await index_document(doc, ollama, meili)
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
    if ollama.embed_retry_count > 0:
        log.warning(
            "embedding retries occurred — lower EMBED_MAX_CHARS to avoid extra round-trips",
            retries=ollama.embed_retry_count,
            current_embed_max_chars=settings.embed_max_chars,
        )

    # Persist "index built successfully" marker so poll_inbox knows it can run
    if count > 0:
        with get_conn() as conn:
            conn.execute(
                "INSERT INTO audit_log (action, actor, details) VALUES (?, ?, ?)",
                ("index_complete", "system", f"indexed={count}"),
            )

    return count


async def reindex_all(
    paperless: PaperlessClient,
    ollama: OllamaClient,
    meili: MeiliClient,
) -> int:
    """Drop all embeddings and rebuild from scratch.

    Use this when the embedding model changes.
    """
    try:
        log.info("starting full reindex — clearing existing embeddings")
        await meili.delete_all_documents()
        with get_conn() as conn:
            conn.execute("DELETE FROM doc_embedding_meta")
            conn.execute("DELETE FROM doc_embeddings")

        # --- Phase 0: OCR correction (before embedding) ---
        ocr_mode = effective_ocr_mode()
        if ocr_mode != "off":
            log.info("reindex phase ocr — correcting documents", mode=ocr_mode)
            docs = await paperless.list_all_documents()
            corrected = 0
            for doc in docs:
                if _reindex_progress.cancelled:
                    log.info("reindex cancelled during OCR phase")
                    break
                if get_cached_ocr(doc.id) is not None:
                    continue  # already cached from a previous run
                try:
                    text, num = await maybe_correct_ocr(doc, ollama, paperless)
                    if num > 0 or ocr_mode.startswith("vision"):
                        cache_ocr_correction(doc.id, text, ocr_mode, num)
                        corrected += 1
                except Exception as exc:
                    log.warning("reindex ocr failed", doc_id=doc.id, error=str(exc))
            log.info("reindex phase ocr complete", corrected=corrected, total=len(docs))

            # Unload OCR/vision model before embedding phase
            if ocr_mode == "text":
                await ollama.unload_model(ollama.ocr_model)
            else:
                vision_model = settings.ocr_vision_model or ollama.model
                await ollama.unload_model(vision_model)

        # --- Phase 1: Embedding (uses cached OCR text if available) ---
        result = await initial_index(paperless, ollama, meili)
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
    meili: MeiliClient,
) -> bool:
    """Launch ``reindex_all`` as a background asyncio task.

    Returns ``True`` if started, ``False`` if already running.
    """
    if _reindex_progress.running:
        return False

    # Initialise progress BEFORE creating the task so the HTTP response
    # immediately sees running=True (fixes the race condition).
    _reindex_progress.running = True
    _reindex_progress.total = 0
    _reindex_progress.done = 0
    _reindex_progress.failed = 0
    _reindex_progress.started_at = datetime.now(tz=UTC).isoformat()
    _reindex_progress.finished_at = None
    _reindex_progress.error = None
    _reindex_progress.cancelled = False

    async def _run() -> None:
        try:
            await reindex_all(paperless, ollama, meili)
        except Exception as exc:
            log.error("background reindex failed", error=str(exc))

    global _reindex_task
    _reindex_task = asyncio.create_task(_run())
    return True
