"""Error log routes with retry capability."""

from __future__ import annotations

import structlog
from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse

from app.db import get_conn

log = structlog.get_logger(__name__)
router = APIRouter(prefix="/errors")


@router.get("")
async def error_list(request: Request):
    with get_conn() as conn:
        rows = conn.execute("SELECT * FROM errors ORDER BY occurred_at DESC LIMIT 100").fetchall()
    errors = [dict(r) for r in rows]
    return request.app.state.templates.TemplateResponse(
        "errors.html",
        {"request": request, "errors": errors},
    )


@router.post("/{error_id}/retry")
async def retry_error(request: Request, error_id: int):
    with get_conn() as conn:
        row = conn.execute("SELECT document_id FROM errors WHERE id = ?", (error_id,)).fetchone()
        if not row or not row["document_id"]:
            return HTMLResponse("Error not found or no document_id", status_code=404)

        doc_id = row["document_id"]
        conn.execute(
            "DELETE FROM processed_documents WHERE document_id = ?",
            (doc_id,),
        )
        conn.execute(
            """
            INSERT INTO audit_log (action, document_id, actor, details)
            VALUES ('retry', ?, 'user', 'Retried after error')
            """,
            (doc_id,),
        )

    log.info("retry requested", doc_id=doc_id, error_id=error_id)
    return HTMLResponse(
        f'<tr id="error-{error_id}" class="bg-yellow-50">'
        f'<td colspan="5" class="px-4 py-3 text-yellow-700 text-center">'
        f"Retry queued — document will be reprocessed in next poll cycle</td></tr>"
    )
