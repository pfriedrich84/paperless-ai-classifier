"""Write accepted suggestions back to Paperless-NGX via PATCH."""

from __future__ import annotations

import json

import httpx
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


async def retroactive_tag_apply(
    tag_name: str,
    paperless_id: int,
    paperless: PaperlessClient,
) -> tuple[int, int]:
    """Retroactively apply a newly approved tag to affected suggestions.

    Finds all suggestions that proposed *tag_name* with ``"id": null``,
    resolves the ID, and — for already-committed documents — PATCHes
    Paperless to add the tag.

    Returns ``(patched_docs, updated_pending)`` counts.
    """
    # Find candidate suggestions (both committed and pending)
    with get_conn() as conn:
        rows = conn.execute(
            """SELECT id, document_id, status, proposed_tags_json
               FROM suggestions
               WHERE proposed_tags_json LIKE ?
                 AND status IN ('committed', 'pending')""",
            (f"%{tag_name}%",),
        ).fetchall()

    patched_docs = 0
    updated_pending = 0

    for row in rows:
        try:
            tags = json.loads(row["proposed_tags_json"])
        except (json.JSONDecodeError, TypeError):
            continue

        # Find entries matching this tag name with unresolved id
        changed = False
        for entry in tags:
            if entry.get("name", "").lower() == tag_name.lower() and entry.get("id") is None:
                entry["id"] = paperless_id
                changed = True

        if not changed:
            continue

        updated_json = json.dumps(tags, ensure_ascii=False)

        # Update the suggestion record
        with get_conn() as conn:
            conn.execute(
                "UPDATE suggestions SET proposed_tags_json = ? WHERE id = ?",
                (updated_json, row["id"]),
            )

        if row["status"] == "pending":
            updated_pending += 1
            log.debug(
                "tag resolved in pending suggestion",
                suggestion_id=row["id"],
                tag=tag_name,
            )
            continue

        # For committed suggestions: PATCH Paperless to add the tag
        doc_id = row["document_id"]
        try:
            doc = await paperless.get_document(doc_id)
            if paperless_id in doc.tags:
                continue  # already has the tag
            new_tags = sorted(set(doc.tags) | {paperless_id})
            await paperless.patch_document(doc_id, {"tags": new_tags})
            patched_docs += 1
            log.info(
                "tag applied retroactively",
                doc_id=doc_id,
                tag=tag_name,
                paperless_id=paperless_id,
            )

            with get_conn() as conn:
                conn.execute(
                    """INSERT INTO audit_log (action, document_id, actor, details)
                       VALUES ('retroactive_tag', ?, 'system', ?)""",
                    (
                        doc_id,
                        json.dumps(
                            {"tag_name": tag_name, "paperless_id": paperless_id},
                            ensure_ascii=False,
                        ),
                    ),
                )
        except httpx.HTTPStatusError as exc:
            if exc.response.status_code == 404:
                log.warning("document gone, skipping retroactive tag", doc_id=doc_id)
            else:
                log.warning("retroactive tag patch failed", doc_id=doc_id, error=str(exc))
        except Exception as exc:
            log.warning("retroactive tag patch failed", doc_id=doc_id, error=str(exc))

    return patched_docs, updated_pending


async def retroactive_correspondent_apply(
    corr_name: str,
    paperless_id: int,
    paperless: PaperlessClient,
) -> tuple[int, int]:
    """Retroactively apply a newly approved correspondent to affected suggestions.

    Finds all suggestions that proposed *corr_name* with ``proposed_correspondent_id = NULL``,
    resolves the ID, and — for already-committed documents — PATCHes
    Paperless to set the correspondent.

    Returns ``(patched_docs, updated_pending)`` counts.
    """
    with get_conn() as conn:
        rows = conn.execute(
            """SELECT id, document_id, status, proposed_correspondent_name
               FROM suggestions
               WHERE proposed_correspondent_name = ?
                 AND proposed_correspondent_id IS NULL
                 AND status IN ('committed', 'pending')""",
            (corr_name,),
        ).fetchall()

    patched_docs = 0
    updated_pending = 0

    for row in rows:
        # Update the suggestion record with the resolved ID
        with get_conn() as conn:
            conn.execute(
                "UPDATE suggestions SET proposed_correspondent_id = ? WHERE id = ?",
                (paperless_id, row["id"]),
            )

        if row["status"] == "pending":
            updated_pending += 1
            log.debug(
                "correspondent resolved in pending suggestion",
                suggestion_id=row["id"],
                correspondent=corr_name,
            )
            continue

        # For committed suggestions: PATCH Paperless to set the correspondent
        doc_id = row["document_id"]
        try:
            doc = await paperless.get_document(doc_id)
            if doc.correspondent == paperless_id:
                continue  # already has this correspondent
            await paperless.patch_document(doc_id, {"correspondent": paperless_id})
            patched_docs += 1
            log.info(
                "correspondent applied retroactively",
                doc_id=doc_id,
                correspondent=corr_name,
                paperless_id=paperless_id,
            )

            with get_conn() as conn:
                conn.execute(
                    """INSERT INTO audit_log (action, document_id, actor, details)
                       VALUES ('retroactive_correspondent', ?, 'system', ?)""",
                    (
                        doc_id,
                        json.dumps(
                            {"correspondent_name": corr_name, "paperless_id": paperless_id},
                            ensure_ascii=False,
                        ),
                    ),
                )
        except httpx.HTTPStatusError as exc:
            if exc.response.status_code == 404:
                log.warning("document gone, skipping retroactive correspondent", doc_id=doc_id)
            else:
                log.warning("retroactive correspondent patch failed", doc_id=doc_id, error=str(exc))
        except Exception as exc:
            log.warning("retroactive correspondent patch failed", doc_id=doc_id, error=str(exc))

    return patched_docs, updated_pending


async def retroactive_doctype_apply(
    doctype_name: str,
    paperless_id: int,
    paperless: PaperlessClient,
) -> tuple[int, int]:
    """Retroactively apply a newly approved document type to affected suggestions.

    Finds all suggestions that proposed *doctype_name* with ``proposed_doctype_id = NULL``,
    resolves the ID, and — for already-committed documents — PATCHes
    Paperless to set the document type.

    Returns ``(patched_docs, updated_pending)`` counts.
    """
    with get_conn() as conn:
        rows = conn.execute(
            """SELECT id, document_id, status, proposed_doctype_name
               FROM suggestions
               WHERE proposed_doctype_name = ?
                 AND proposed_doctype_id IS NULL
                 AND status IN ('committed', 'pending')""",
            (doctype_name,),
        ).fetchall()

    patched_docs = 0
    updated_pending = 0

    for row in rows:
        with get_conn() as conn:
            conn.execute(
                "UPDATE suggestions SET proposed_doctype_id = ? WHERE id = ?",
                (paperless_id, row["id"]),
            )

        if row["status"] == "pending":
            updated_pending += 1
            log.debug(
                "doctype resolved in pending suggestion",
                suggestion_id=row["id"],
                doctype=doctype_name,
            )
            continue

        doc_id = row["document_id"]
        try:
            doc = await paperless.get_document(doc_id)
            if doc.document_type == paperless_id:
                continue
            await paperless.patch_document(doc_id, {"document_type": paperless_id})
            patched_docs += 1
            log.info(
                "doctype applied retroactively",
                doc_id=doc_id,
                doctype=doctype_name,
                paperless_id=paperless_id,
            )

            with get_conn() as conn:
                conn.execute(
                    """INSERT INTO audit_log (action, document_id, actor, details)
                       VALUES ('retroactive_doctype', ?, 'system', ?)""",
                    (
                        doc_id,
                        json.dumps(
                            {"doctype_name": doctype_name, "paperless_id": paperless_id},
                            ensure_ascii=False,
                        ),
                    ),
                )
        except httpx.HTTPStatusError as exc:
            if exc.response.status_code == 404:
                log.warning("document gone, skipping retroactive doctype", doc_id=doc_id)
            else:
                log.warning("retroactive doctype patch failed", doc_id=doc_id, error=str(exc))
        except Exception as exc:
            log.warning("retroactive doctype patch failed", doc_id=doc_id, error=str(exc))

    return patched_docs, updated_pending


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
