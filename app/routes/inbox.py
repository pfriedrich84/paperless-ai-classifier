"""Inbox routes — show documents with Posteingang tag and allow reprocessing."""

from __future__ import annotations

import structlog
from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse

from app.config import settings
from app.db import get_conn
from app.worker import _process_document

log = structlog.get_logger(__name__)
router = APIRouter(prefix="/inbox")


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


@router.get("")
async def inbox_list(request: Request):
    paperless = request.app.state.paperless
    try:
        docs = await paperless.list_inbox_documents(settings.paperless_inbox_tag_id)
    except Exception as exc:
        log.error("failed to fetch inbox", error=str(exc))
        docs = []

    # Enrich with processing status
    items = []
    for doc in docs:
        items.append({
            "id": doc.id,
            "title": doc.title,
            "created_date": doc.created_date,
            "added": doc.added.isoformat()[:16] if doc.added else None,
            "status": _get_processing_status(doc.id),
        })

    return request.app.state.templates.TemplateResponse(
        request,
        "inbox.html",
        {"items": items},
    )


@router.post("/{document_id}/reprocess")
async def reprocess_document(request: Request, document_id: int):
    """Clear previous processing state and re-run the classification pipeline."""
    log.info("reprocessing document", doc_id=document_id)

    paperless = request.app.state.paperless
    ollama = request.app.state.ollama

    # Clear previous processing state so _process_document doesn't skip it
    with get_conn() as conn:
        conn.execute(
            "DELETE FROM processed_documents WHERE document_id = ?",
            (document_id,),
        )

    try:
        doc = await paperless.get_document(document_id)
    except Exception as exc:
        log.error("failed to fetch document", doc_id=document_id, error=str(exc))
        return HTMLResponse(
            _render_row(document_id, "(fetch failed)", None, None, "error")
            + f'<div id="reprocess-msg-{document_id}" class="text-red-600 text-xs mt-1">'
            f"Failed: {exc}</div>",
        )

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
        log.error("reprocess failed", doc_id=document_id, error=str(exc))
        status = _get_processing_status(document_id)
        return HTMLResponse(
            _render_row(document_id, doc.title, doc.created_date, doc.added, status)
        )

    status = _get_processing_status(document_id)
    return HTMLResponse(
        _render_row(document_id, doc.title, doc.created_date, doc.added, status)
    )


def _status_badge(status: str) -> str:
    colors = {
        "unprocessed": "bg-gray-100 text-gray-700",
        "pending": "bg-amber-100 text-amber-800",
        "committed": "bg-green-100 text-green-800",
        "accepted": "bg-green-100 text-green-800",
        "rejected": "bg-red-100 text-red-800",
        "error": "bg-red-100 text-red-800",
    }
    css = colors.get(status, "bg-gray-100 text-gray-700")
    return (
        f'<span class="inline-flex items-center px-2.5 py-0.5 rounded-full'
        f' text-xs font-medium {css}">{status}</span>'
    )


def _render_row(
    doc_id: int,
    title: str,
    created_date: str | None,
    added: object | None,
    status: str,
) -> str:
    added_str = added.isoformat()[:16] if hasattr(added, "isoformat") else (str(added)[:16] if added else "—")
    badge = _status_badge(status)
    return (
        f'<tr id="doc-{doc_id}" class="hover:bg-gray-50 transition-colors">'
        f'<td class="px-4 py-3 text-sm font-medium text-gray-900">#{doc_id}</td>'
        f'<td class="px-4 py-3 text-sm text-gray-800">{title or "—"}</td>'
        f'<td class="px-4 py-3 text-sm text-gray-500">{created_date or "—"}</td>'
        f'<td class="px-4 py-3 text-sm text-gray-500">{added_str}</td>'
        f'<td class="px-4 py-3 text-center">{badge}</td>'
        f'<td class="px-4 py-3 text-right">'
        f'<button hx-post="/inbox/{doc_id}/reprocess"'
        f' hx-target="#doc-{doc_id}"'
        f' hx-swap="outerHTML"'
        f' hx-indicator="#spinner-{doc_id}"'
        f' class="inline-flex items-center px-3 py-1.5 text-xs font-medium rounded-lg'
        f' bg-primary-50 text-primary-700 hover:bg-primary-100 transition-colors">'
        f'<svg id="spinner-{doc_id}" class="htmx-indicator animate-spin -ml-0.5 mr-1.5 h-3 w-3" fill="none" viewBox="0 0 24 24">'
        f'<circle class="opacity-25" cx="12" cy="12" r="10" stroke="currentColor" stroke-width="4"></circle>'
        f'<path class="opacity-75" fill="currentColor" d="M4 12a8 8 0 018-8V0C5.373 0 0 5.373 0 12h4z"></path>'
        f"</svg>"
        f"Reprocess"
        f"</button>"
        f"</td></tr>"
    )
