"""Inbox routes — show documents with Posteingang tag, reprocess, bulk actions."""

from __future__ import annotations

import asyncio
import json
from collections import Counter
from dataclasses import dataclass

import structlog
from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse

from app.config import settings
from app.db import get_conn
from app.worker import _process_document

log = structlog.get_logger(__name__)
router = APIRouter(prefix="/inbox")


# ---------------------------------------------------------------------------
# Bulk processing progress (module-level, like app/indexer.py)
# ---------------------------------------------------------------------------
@dataclass
class BulkProcessProgress:
    running: bool = False
    total: int = 0
    done: int = 0
    succeeded: int = 0
    failed: int = 0
    mode: str = ""  # "inbox" or "all"


_bulk_progress = BulkProcessProgress()
_bulk_task: asyncio.Task | None = None
_reprocess_tasks: set[asyncio.Task] = set()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def _get_processing_status(doc_id: int) -> str:
    """Look up the processing status for a document."""
    with get_conn() as conn:
        row = conn.execute(
            "SELECT status FROM processed_documents WHERE document_id = ?",
            (doc_id,),
        ).fetchone()
    if not row:
        return "unprocessed"
    return row["status"]


def _get_suggestion_for_doc(doc_id: int) -> dict | None:
    """Return the latest suggestion for a document, or None."""
    with get_conn() as conn:
        row = conn.execute(
            """SELECT s.id, s.status, s.confidence, s.reasoning,
                      s.proposed_title, s.proposed_correspondent_name,
                      s.proposed_doctype_name, s.proposed_storage_path_name,
                      s.proposed_tags_json
               FROM suggestions s
               WHERE s.document_id = ?
               ORDER BY s.created_at DESC LIMIT 1""",
            (doc_id,),
        ).fetchone()
    if not row:
        return None
    return dict(row)


def _content_snippet(text: str | None, max_len: int = 150) -> str:
    """Trim content to ~max_len chars at a word boundary."""
    if not text:
        return ""
    text = text.strip().replace("\n", " ").replace("\r", "")
    # collapse multiple spaces
    while "  " in text:
        text = text.replace("  ", " ")
    if len(text) <= max_len:
        return text
    cut = text[:max_len].rsplit(" ", 1)[0]
    return cut + "..."


def _build_item(doc, status: str, suggestion: dict | None,
                corr_lookup: dict, dt_lookup: dict, tag_lookup: dict,
                paperless_url: str) -> dict:
    """Build an enriched item dict for a single document."""
    return {
        "id": doc.id,
        "title": doc.title,
        "content_snippet": _content_snippet(doc.content),
        "created_date": doc.created_date,
        "added": doc.added.isoformat()[:16] if doc.added else None,
        "status": status,
        "paperless_url": paperless_url,
        # suggestion data
        "suggestion_id": suggestion["id"] if suggestion else None,
        "proposed_title": suggestion["proposed_title"] if suggestion else None,
        "proposed_correspondent": suggestion["proposed_correspondent_name"] if suggestion else None,
        "proposed_doctype": suggestion["proposed_doctype_name"] if suggestion else None,
        "confidence": suggestion["confidence"] if suggestion else None,
        # resolved entity names
        "correspondent_name": corr_lookup.get(doc.correspondent),
        "doctype_name": dt_lookup.get(doc.document_type),
        "tag_names": [tag_lookup[t] for t in (doc.tags or []) if t in tag_lookup],
    }


def _render_card(request: Request, item: dict) -> str:
    """Render a single inbox card via the Jinja partial."""
    tmpl = request.app.state.templates.get_template("partials/inbox_card.html")
    return tmpl.render(item=item)


async def _fetch_entity_lookups(paperless):
    """Fetch entity lists and build {id: name} lookups."""
    correspondents = await paperless.list_correspondents()
    doctypes = await paperless.list_document_types()
    tags = await paperless.list_tags()
    corr_lookup = {c.id: c.name for c in correspondents}
    dt_lookup = {d.id: d.name for d in doctypes}
    tag_lookup = {t.id: t.name for t in tags}
    return correspondents, doctypes, tags, corr_lookup, dt_lookup, tag_lookup


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------
@router.get("")
async def inbox_list(request: Request):
    paperless = request.app.state.paperless
    paperless_url = settings.paperless_url.rstrip("/")

    try:
        docs = await paperless.list_inbox_documents(settings.paperless_inbox_tag_id)
    except Exception as exc:
        log.error("failed to fetch inbox", error=str(exc))
        docs = []

    # Fetch entity lookups for name resolution
    try:
        _, _, _, corr_lookup, dt_lookup, tag_lookup = await _fetch_entity_lookups(paperless)
    except Exception as exc:
        log.error("failed to fetch entity lists", error=str(exc))
        corr_lookup, dt_lookup, tag_lookup = {}, {}, {}

    # Enrich with processing status + suggestion data
    items = []
    for doc in docs:
        status = _get_processing_status(doc.id)
        suggestion = _get_suggestion_for_doc(doc.id)
        items.append(_build_item(
            doc, status, suggestion,
            corr_lookup, dt_lookup, tag_lookup, paperless_url,
        ))

    # Status counts
    counts = Counter(item["status"] for item in items)

    return request.app.state.templates.TemplateResponse(
        request,
        "inbox.html",
        {
            "items": items,
            "counts": counts,
            "bulk_progress": _bulk_progress,
        },
    )


@router.get("/{document_id}/status")
async def document_status(request: Request, document_id: int):
    """Return the current card HTML for a single document (used by HTMX polling)."""
    paperless = request.app.state.paperless
    paperless_url = settings.paperless_url.rstrip("/")

    status = _get_processing_status(document_id)
    suggestion = _get_suggestion_for_doc(document_id)

    try:
        doc = await paperless.get_document(document_id)
    except Exception:
        # Minimal fallback
        item = {
            "id": document_id,
            "title": "(unavailable)",
            "content_snippet": "",
            "created_date": None,
            "added": None,
            "status": status,
            "paperless_url": paperless_url,
            "suggestion_id": suggestion["id"] if suggestion else None,
            "proposed_title": suggestion["proposed_title"] if suggestion else None,
            "proposed_correspondent": suggestion["proposed_correspondent_name"] if suggestion else None,
            "proposed_doctype": suggestion["proposed_doctype_name"] if suggestion else None,
            "confidence": suggestion["confidence"] if suggestion else None,
            "correspondent_name": None,
            "doctype_name": None,
            "tag_names": [],
        }
        return HTMLResponse(_render_card(request, item))

    try:
        _, _, _, corr_lookup, dt_lookup, tag_lookup = await _fetch_entity_lookups(paperless)
    except Exception:
        corr_lookup, dt_lookup, tag_lookup = {}, {}, {}

    item = _build_item(doc, status, suggestion,
                       corr_lookup, dt_lookup, tag_lookup, paperless_url)

    return HTMLResponse(_render_card(request, item))


@router.post("/{document_id}/reprocess")
async def reprocess_document(request: Request, document_id: int):
    """Clear previous state and re-run classification in the background."""
    log.info("reprocessing document", doc_id=document_id)

    paperless = request.app.state.paperless
    ollama = request.app.state.ollama
    paperless_url = settings.paperless_url.rstrip("/")

    # Clear previous processing state
    with get_conn() as conn:
        conn.execute(
            "DELETE FROM processed_documents WHERE document_id = ?",
            (document_id,),
        )

    # Fetch the document to render the card
    try:
        doc = await paperless.get_document(document_id)
    except Exception as exc:
        log.error("failed to fetch document", doc_id=document_id, error=str(exc))
        item = {
            "id": document_id,
            "title": "(fetch failed)",
            "content_snippet": "",
            "created_date": None,
            "added": None,
            "status": "error",
            "paperless_url": paperless_url,
            "suggestion_id": None,
            "proposed_title": None,
            "proposed_correspondent": None,
            "proposed_doctype": None,
            "confidence": None,
            "correspondent_name": None,
            "doctype_name": None,
            "tag_names": [],
        }
        response = HTMLResponse(_render_card(request, item))
        response.headers["HX-Trigger"] = json.dumps({
            "showToast": {"message": f"Failed to fetch document #{document_id}", "type": "error"}
        })
        return response

    # Mark as processing immediately (use empty last_updated_at so
    # _process_document's idempotency check won't skip it)
    with get_conn() as conn:
        conn.execute(
            """INSERT OR REPLACE INTO processed_documents
                (document_id, last_updated_at, last_processed, status)
            VALUES (?, '', datetime('now'), 'processing')""",
            (document_id,),
        )

    # Launch background task
    async def _run_reprocess():
        try:
            correspondents = await paperless.list_correspondents()
            doctypes = await paperless.list_document_types()
            storage_paths = await paperless.list_storage_paths()
            tags = await paperless.list_tags()
            await _process_document(
                doc, paperless, ollama,
                correspondents, doctypes, storage_paths, tags,
            )
        except Exception as exc:
            log.error("background reprocess failed", doc_id=document_id, error=str(exc))
            with get_conn() as conn:
                conn.execute(
                    """INSERT OR REPLACE INTO processed_documents
                        (document_id, last_updated_at, last_processed, status)
                    VALUES (?, ?, datetime('now'), 'error')""",
                    (document_id, ""),
                )

    task = asyncio.create_task(_run_reprocess())
    _reprocess_tasks.add(task)
    task.add_done_callback(_reprocess_tasks.discard)

    # Return card in processing state immediately
    try:
        _, _, _, corr_lookup, dt_lookup, tag_lookup = await _fetch_entity_lookups(paperless)
    except Exception:
        corr_lookup, dt_lookup, tag_lookup = {}, {}, {}

    suggestion = _get_suggestion_for_doc(document_id)
    item = _build_item(doc, "processing", suggestion,
                       corr_lookup, dt_lookup, tag_lookup, paperless_url)

    response = HTMLResponse(_render_card(request, item))
    response.headers["HX-Trigger"] = json.dumps({
        "showToast": {"message": f"Processing document #{document_id}...", "type": "info"}
    })
    return response


# ---------------------------------------------------------------------------
# Bulk processing
# ---------------------------------------------------------------------------
@router.post("/process-inbox")
async def process_inbox_bulk(request: Request):
    """Process all unprocessed/error inbox documents in the background."""
    global _bulk_task, _bulk_progress

    if _bulk_progress.running:
        return HTMLResponse(_render_bulk_progress())

    paperless = request.app.state.paperless
    ollama = request.app.state.ollama

    try:
        docs = await paperless.list_inbox_documents(settings.paperless_inbox_tag_id)
    except Exception as exc:
        log.error("bulk process-inbox: failed to fetch inbox", error=str(exc))
        return HTMLResponse(
            '<div class="text-red-600 text-sm font-medium mt-2">'
            f"Failed to fetch inbox: {exc}</div>"
        )

    # Filter to unprocessed / error only
    to_process = [d for d in docs if _get_processing_status(d.id) in ("unprocessed", "error")]

    if not to_process:
        return HTMLResponse(
            '<div class="text-gray-500 text-sm mt-2">No unprocessed documents to process.</div>'
        )

    _bulk_progress = BulkProcessProgress(
        running=True, total=len(to_process), mode="inbox",
    )

    async def _run():
        try:
            correspondents = await paperless.list_correspondents()
            doctypes = await paperless.list_document_types()
            storage_paths = await paperless.list_storage_paths()
            tags = await paperless.list_tags()

            for doc in to_process:
                try:
                    # Mark processing (empty last_updated_at so _process_document won't skip)
                    with get_conn() as conn:
                        conn.execute(
                            """INSERT OR REPLACE INTO processed_documents
                                (document_id, last_updated_at, last_processed, status)
                            VALUES (?, '', datetime('now'), 'processing')""",
                            (doc.id,),
                        )
                    await _process_document(
                        doc, paperless, ollama,
                        correspondents, doctypes, storage_paths, tags,
                    )
                    _bulk_progress.succeeded += 1
                except Exception as exc:
                    log.error("bulk process failed for doc", doc_id=doc.id, error=str(exc))
                    _bulk_progress.failed += 1
                    with get_conn() as conn:
                        conn.execute(
                            """INSERT OR REPLACE INTO processed_documents
                                (document_id, last_updated_at, last_processed, status)
                            VALUES (?, ?, datetime('now'), 'error')""",
                            (doc.id, ""),
                        )
                finally:
                    _bulk_progress.done += 1
        finally:
            _bulk_progress.running = False

    _bulk_task = asyncio.create_task(_run())
    return HTMLResponse(_render_bulk_progress())


@router.post("/process-all")
async def process_all_docs(request: Request):
    """Process ALL documents in Paperless (not just inbox) in the background."""
    global _bulk_task, _bulk_progress

    if _bulk_progress.running:
        return HTMLResponse(_render_bulk_progress())

    paperless = request.app.state.paperless
    ollama = request.app.state.ollama

    try:
        docs = await paperless.list_all_documents()
    except Exception as exc:
        log.error("bulk process-all: failed to fetch documents", error=str(exc))
        return HTMLResponse(
            '<div class="text-red-600 text-sm font-medium mt-2">'
            f"Failed to fetch documents: {exc}</div>"
        )

    if not docs:
        return HTMLResponse(
            '<div class="text-gray-500 text-sm mt-2">No documents found.</div>'
        )

    # Filter to docs not already successfully processed
    to_process = [d for d in docs if _get_processing_status(d.id) in ("unprocessed", "error")]

    if not to_process:
        return HTMLResponse(
            '<div class="text-gray-500 text-sm mt-2">All documents already processed.</div>'
        )

    _bulk_progress = BulkProcessProgress(
        running=True, total=len(to_process), mode="all",
    )

    async def _run():
        try:
            correspondents = await paperless.list_correspondents()
            doctypes = await paperless.list_document_types()
            storage_paths = await paperless.list_storage_paths()
            tags = await paperless.list_tags()

            for doc in to_process:
                try:
                    # Mark processing (empty last_updated_at so _process_document won't skip)
                    with get_conn() as conn:
                        conn.execute(
                            """INSERT OR REPLACE INTO processed_documents
                                (document_id, last_updated_at, last_processed, status)
                            VALUES (?, '', datetime('now'), 'processing')""",
                            (doc.id,),
                        )
                    await _process_document(
                        doc, paperless, ollama,
                        correspondents, doctypes, storage_paths, tags,
                    )
                    _bulk_progress.succeeded += 1
                except Exception as exc:
                    log.error("bulk process-all failed for doc", doc_id=doc.id, error=str(exc))
                    _bulk_progress.failed += 1
                    with get_conn() as conn:
                        conn.execute(
                            """INSERT OR REPLACE INTO processed_documents
                                (document_id, last_updated_at, last_processed, status)
                            VALUES (?, ?, datetime('now'), 'error')""",
                            (doc.id, ""),
                        )
                finally:
                    _bulk_progress.done += 1
        finally:
            _bulk_progress.running = False

    _bulk_task = asyncio.create_task(_run())
    return HTMLResponse(_render_bulk_progress())


@router.get("/bulk-status")
async def bulk_status(request: Request):
    """Return bulk progress HTML for HTMX polling."""
    return HTMLResponse(_render_bulk_progress())


def _render_bulk_progress() -> str:
    """Build an HTML fragment for the bulk processing progress area."""
    p = _bulk_progress

    if p.running:
        pct = int(p.done / p.total * 100) if p.total > 0 else 0
        mode_label = "inbox documents" if p.mode == "inbox" else "all documents"
        return (
            '<div id="bulk-progress" hx-get="/inbox/bulk-status"'
            ' hx-trigger="every 2s" hx-swap="outerHTML">'
            '<div class="mt-3 bg-blue-50 border border-blue-200 rounded-lg p-4">'
            '<div class="flex justify-between text-sm text-blue-800 mb-2">'
            f'<span>Processing {mode_label}...</span>'
            f'<span>{p.done} / {p.total}'
            f'{" (" + str(p.failed) + " failed)" if p.failed else ""}</span>'
            '</div>'
            '<div class="w-full bg-blue-200 rounded-full h-2.5">'
            '<div class="bg-blue-600 h-2.5 rounded-full transition-all duration-500"'
            f' style="width: {pct}%"></div>'
            '</div>'
            '</div></div>'
        )

    if p.total > 0:
        # Just finished
        success = p.succeeded
        failed = p.failed
        css = "bg-green-50 border-green-200 text-green-800" if failed == 0 else "bg-amber-50 border-amber-200 text-amber-800"
        return (
            f'<div id="bulk-progress">'
            f'<div class="mt-3 {css} border rounded-lg p-4 text-sm">'
            f'Done: {success} processed'
            f'{", " + str(failed) + " failed" if failed else ""}'
            f' &mdash; <a href="/inbox" class="underline font-medium">Refresh page</a>'
            f'</div></div>'
        )

    return '<div id="bulk-progress"></div>'
