"""Write accepted suggestions back to Paperless-NGX via PATCH."""

from __future__ import annotations

import json

import structlog

from app.clients.paperless import PaperlessClient
from app.config import settings
from app.db import get_conn
from app.models import ReviewDecision, SuggestionRow

log = structlog.get_logger(__name__)


async def commit_suggestion(
    suggestion: SuggestionRow,
    decision: ReviewDecision,
    paperless: PaperlessClient,
) -> None:
    """Apply a reviewed suggestion to Paperless and update local state.

    On error the exception is swallowed — an error record is written to the DB
    and the suggestion is marked as ``error`` so the worker keeps running.
    """
    doc_id = suggestion.document_id
    try:
        # -- 1. Build PATCH fields ----------------------------------------
        fields: dict[str, object] = {"title": decision.title}
        if decision.date:
            fields["created_date"] = decision.date
        if decision.correspondent_id is not None:
            fields["correspondent"] = decision.correspondent_id
        if decision.doctype_id is not None:
            fields["document_type"] = decision.doctype_id
        if decision.storage_path_id is not None:
            fields["storage_path"] = decision.storage_path_id

        # -- 2. Merge tags ------------------------------------------------
        doc = await paperless.get_document(doc_id)
        tag_set = set(doc.tags)
        if not settings.keep_inbox_tag:
            tag_set.discard(settings.paperless_inbox_tag_id)
        tag_set.update(decision.tag_ids)
        if settings.paperless_processed_tag_id:
            tag_set.add(settings.paperless_processed_tag_id)
        fields["tags"] = sorted(tag_set)

        # -- 3. PATCH ------------------------------------------------------
        await paperless.patch_document(doc_id, fields)

        # -- 4. Update DB -------------------------------------------------
        with get_conn() as conn:
            conn.execute(
                "UPDATE suggestions SET status = 'committed' WHERE id = ?",
                (suggestion.id,),
            )
            conn.execute(
                "UPDATE processed_documents SET status = 'committed' WHERE document_id = ?",
                (doc_id,),
            )

            # -- 5. Audit log ---------------------------------------------
            conn.execute(
                """
                INSERT INTO audit_log (action, document_id, actor, details)
                VALUES ('commit', ?, 'system', ?)
                """,
                (doc_id, json.dumps(fields, default=str, ensure_ascii=False)),
            )

        log.info("suggestion committed", doc_id=doc_id, suggestion_id=suggestion.id)

    except Exception as exc:
        log.warning(
            "commit failed",
            doc_id=doc_id,
            suggestion_id=suggestion.id,
            error=str(exc),
        )
        _record_error(doc_id, suggestion.id, exc)


def _record_error(doc_id: int, suggestion_id: int, exc: Exception) -> None:
    """Persist commit failure to DB without raising."""
    try:
        with get_conn() as conn:
            conn.execute(
                """
                INSERT INTO errors (stage, document_id, message, details)
                VALUES ('commit', ?, ?, ?)
                """,
                (doc_id, str(exc), None),
            )
            conn.execute(
                "UPDATE suggestions SET status = 'error' WHERE id = ?",
                (suggestion_id,),
            )
            conn.execute(
                "UPDATE processed_documents SET status = 'error' WHERE document_id = ?",
                (doc_id,),
            )
    except Exception as inner:
        log.error("failed to record commit error", error=str(inner))
