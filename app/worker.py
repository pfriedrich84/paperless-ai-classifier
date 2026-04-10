"""APScheduler-based background worker for inbox polling and classification."""

from __future__ import annotations

import json
from datetime import UTC, datetime

import structlog
from apscheduler.schedulers.asyncio import AsyncIOScheduler

from app.clients.ollama import OllamaClient
from app.clients.paperless import PaperlessClient
from app.config import settings
from app.db import get_conn
from app.models import (
    ClassificationResult,
    PaperlessDocument,
    PaperlessEntity,
    ReviewDecision,
    SuggestionRow,
)
from app.pipeline import classifier, context_builder
from app.pipeline.committer import commit_suggestion
from app.pipeline.ocr_correction import maybe_correct_ocr
from app.telegram_handler import notify_suggestion

log = structlog.get_logger(__name__)

# Module-level refs set by start_scheduler
_paperless: PaperlessClient | None = None
_ollama: OllamaClient | None = None


# ---------------------------------------------------------------------------
# Entity name → ID resolution
# ---------------------------------------------------------------------------
def _resolve_entity(name: str | None, entities: list[PaperlessEntity]) -> int | None:
    """Case-insensitive exact match of *name* against an entity list."""
    if not name:
        return None
    lower = name.lower()
    for e in entities:
        if e.name.lower() == lower:
            return e.id
    return None


def _resolve_tags(
    proposed_tags: list[dict],
    existing_tags: list[PaperlessEntity],
) -> tuple[list[int], list[dict]]:
    """Match proposed tag names against existing tags.

    Returns ``(resolved_ids, all_tag_dicts)`` where *all_tag_dicts* includes
    the resolved ``id`` for known tags and ``null`` for unknown ones.  Unknown
    tags are inserted into ``tag_whitelist``.
    """
    resolved_ids: list[int] = []
    tag_dicts: list[dict] = []

    for pt in proposed_tags:
        name = pt.get("name", "")
        conf = pt.get("confidence", 50)
        tid = _resolve_entity(name, existing_tags)
        tag_dicts.append({"name": name, "confidence": conf, "id": tid})
        if tid is not None:
            resolved_ids.append(tid)
        else:
            _upsert_tag_whitelist(name)

    return resolved_ids, tag_dicts


def _upsert_tag_whitelist(name: str) -> None:
    """Insert a new tag proposal or bump its counter."""
    with get_conn() as conn:
        row = conn.execute(
            "SELECT times_seen FROM tag_whitelist WHERE name = ?", (name,)
        ).fetchone()
        if row:
            conn.execute(
                "UPDATE tag_whitelist SET times_seen = times_seen + 1 WHERE name = ?",
                (name,),
            )
        else:
            conn.execute(
                "INSERT INTO tag_whitelist (name) VALUES (?)",
                (name,),
            )


# ---------------------------------------------------------------------------
# Suggestion storage
# ---------------------------------------------------------------------------
def _store_suggestion(
    doc: PaperlessDocument,
    result: ClassificationResult,
    raw_response: str,
    correspondents: list[PaperlessEntity],
    doctypes: list[PaperlessEntity],
    storage_paths: list[PaperlessEntity],
    existing_tags: list[PaperlessEntity],
) -> SuggestionRow:
    """Persist a classification result to the ``suggestions`` table."""
    corr_id = _resolve_entity(result.correspondent, correspondents)
    dt_id = _resolve_entity(result.document_type, doctypes)
    sp_id = _resolve_entity(result.storage_path, storage_paths)
    _resolved_tag_ids, tag_dicts = _resolve_tags(
        [{"name": t.name, "confidence": t.confidence} for t in result.tags],
        existing_tags,
    )

    with get_conn() as conn:
        cur = conn.execute(
            """
            INSERT INTO suggestions (
                document_id, confidence, reasoning,
                original_title, original_date, original_correspondent,
                original_doctype, original_storage_path, original_tags_json,
                proposed_title, proposed_date,
                proposed_correspondent_name, proposed_correspondent_id,
                proposed_doctype_name, proposed_doctype_id,
                proposed_storage_path_name, proposed_storage_path_id,
                proposed_tags_json, raw_response
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                doc.id,
                result.confidence,
                result.reasoning,
                doc.title,
                doc.created_date,
                doc.correspondent,
                doc.document_type,
                doc.storage_path,
                json.dumps(doc.tags),
                result.title,
                result.date,
                result.correspondent,
                corr_id,
                result.document_type,
                dt_id,
                result.storage_path,
                sp_id,
                json.dumps(tag_dicts, ensure_ascii=False),
                raw_response,
            ),
        )
        suggestion_id = cur.lastrowid

        conn.execute(
            """
            INSERT OR REPLACE INTO processed_documents
                (document_id, last_updated_at, last_processed, status, suggestion_id)
            VALUES (?, ?, datetime('now'), 'pending', ?)
            """,
            (doc.id, (doc.modified or datetime.now(tz=UTC)).isoformat(), suggestion_id),
        )

    return SuggestionRow(
        id=suggestion_id,
        document_id=doc.id,
        created_at=datetime.now(tz=UTC).isoformat(),
        status="pending",
        confidence=result.confidence,
        reasoning=result.reasoning,
        proposed_title=result.title,
        proposed_date=result.date,
        proposed_correspondent_name=result.correspondent,
        proposed_correspondent_id=corr_id,
        proposed_doctype_name=result.document_type,
        proposed_doctype_id=dt_id,
        proposed_storage_path_name=result.storage_path,
        proposed_storage_path_id=sp_id,
        proposed_tags_json=json.dumps(tag_dicts, ensure_ascii=False),
    )


# ---------------------------------------------------------------------------
# Per-document pipeline
# ---------------------------------------------------------------------------
async def _process_document(
    doc: PaperlessDocument,
    paperless: PaperlessClient,
    ollama: OllamaClient,
    correspondents: list[PaperlessEntity],
    doctypes: list[PaperlessEntity],
    storage_paths: list[PaperlessEntity],
    tags: list[PaperlessEntity],
) -> None:
    """Run the full classification pipeline for a single document."""
    doc_id = doc.id

    # Idempotency: skip if already processed at this version
    with get_conn() as conn:
        row = conn.execute(
            "SELECT last_updated_at FROM processed_documents WHERE document_id = ?",
            (doc_id,),
        ).fetchone()
    if row:
        stored_ts = row["last_updated_at"]
        doc_ts = (doc.modified or datetime.now(tz=UTC)).isoformat()
        if stored_ts == doc_ts:
            log.debug("document already processed", doc_id=doc_id)
            return

    log.info("processing document", doc_id=doc_id, title=doc.title[:80])

    # Mark as pending
    with get_conn() as conn:
        conn.execute(
            """
            INSERT OR REPLACE INTO processed_documents
                (document_id, last_updated_at, last_processed, status)
            VALUES (?, ?, datetime('now'), 'pending')
            """,
            (doc_id, (doc.modified or datetime.now(tz=UTC)).isoformat()),
        )

    # Optional OCR correction (modifies content in-memory only)
    text, num_corrections = await maybe_correct_ocr(doc, ollama)
    if num_corrections > 0:
        doc = doc.model_copy(update={"content": text})

    # Context: similar documents
    context_docs = await context_builder.find_similar_documents(doc, paperless, ollama)

    # Classify
    result, raw_response = await classifier.classify(
        doc,
        context_docs,
        correspondents,
        doctypes,
        storage_paths,
        tags,
        ollama,
    )

    # Store suggestion
    suggestion = _store_suggestion(
        doc,
        result,
        raw_response,
        correspondents,
        doctypes,
        storage_paths,
        tags,
    )

    # Notify via Telegram (only if not auto-committing)
    will_auto_commit = (
        settings.auto_commit_confidence > 0 and result.confidence >= settings.auto_commit_confidence
    )
    if not will_auto_commit:
        await notify_suggestion(suggestion)

    # Auto-commit if confidence is high enough
    if will_auto_commit:
        log.info("auto-committing", doc_id=doc_id, confidence=result.confidence)
        tag_ids = [tid for t in result.tags if (tid := _resolve_entity(t.name, tags)) is not None]
        decision = ReviewDecision(
            suggestion_id=suggestion.id,
            title=result.title,
            date=result.date,
            correspondent_id=suggestion.proposed_correspondent_id,
            doctype_id=suggestion.proposed_doctype_id,
            storage_path_id=suggestion.proposed_storage_path_id,
            tag_ids=tag_ids,
            action="accept",
        )
        await commit_suggestion(suggestion, decision, paperless)

    # Index for future context
    await context_builder.index_document(doc, ollama)


# ---------------------------------------------------------------------------
# Main poll loop
# ---------------------------------------------------------------------------
async def poll_inbox() -> None:
    """Fetch inbox documents and run the classification pipeline."""
    if _paperless is None or _ollama is None:
        log.error("worker not initialised — skipping poll")
        return

    log.info("polling inbox")
    try:
        docs = await _paperless.list_inbox_documents(settings.paperless_inbox_tag_id)
    except Exception as exc:
        log.error("failed to fetch inbox", error=str(exc))
        _write_error("poll", None, exc)
        return

    if not docs:
        log.info("inbox empty")
        return

    # Cache entity lists once per cycle
    try:
        correspondents = await _paperless.list_correspondents()
        doctypes = await _paperless.list_document_types()
        storage_paths = await _paperless.list_storage_paths()
        tags = await _paperless.list_tags()
    except Exception as exc:
        log.error("failed to fetch entity lists", error=str(exc))
        _write_error("poll", None, exc)
        return

    for doc in docs:
        try:
            await _process_document(
                doc,
                _paperless,
                _ollama,
                correspondents,
                doctypes,
                storage_paths,
                tags,
            )
        except Exception as exc:
            log.error("pipeline failed for document", doc_id=doc.id, error=str(exc))
            _write_error("classify", doc.id, exc)


def _write_error(stage: str, doc_id: int | None, exc: Exception) -> None:
    try:
        with get_conn() as conn:
            conn.execute(
                "INSERT INTO errors (stage, document_id, message) VALUES (?, ?, ?)",
                (stage, doc_id, str(exc)),
            )
            if doc_id is not None:
                conn.execute(
                    """
                    UPDATE processed_documents SET status = 'error'
                    WHERE document_id = ?
                    """,
                    (doc_id,),
                )
    except Exception as inner:
        log.error("failed to record error", error=str(inner))


# ---------------------------------------------------------------------------
# Scheduler lifecycle
# ---------------------------------------------------------------------------
def start_scheduler(app: object) -> None:
    """Initialise and start the APScheduler."""
    global _paperless, _ollama

    _paperless = getattr(app, "state", app).paperless  # type: ignore[union-attr]
    _ollama = getattr(app, "state", app).ollama  # type: ignore[union-attr]

    scheduler = AsyncIOScheduler()
    scheduler.add_job(
        poll_inbox,
        "interval",
        seconds=settings.poll_interval_seconds,
        id="poll_inbox",
        replace_existing=True,
    )
    scheduler.start()
    app.state.scheduler = scheduler  # type: ignore[union-attr]
    log.info("scheduler started", interval=settings.poll_interval_seconds)


def stop_scheduler(app: object) -> None:
    """Shutdown the APScheduler gracefully."""
    scheduler = getattr(getattr(app, "state", None), "scheduler", None)
    if scheduler:
        scheduler.shutdown(wait=False)
        log.info("scheduler stopped")
