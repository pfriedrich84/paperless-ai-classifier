"""Tag whitelist management routes."""

from __future__ import annotations

import structlog
from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse

from app.db import get_conn

log = structlog.get_logger(__name__)
router = APIRouter(prefix="/tags")


@router.get("")
async def tag_list(request: Request):
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT * FROM tag_whitelist ORDER BY approved ASC, times_seen DESC"
        ).fetchall()
    tags = [dict(r) for r in rows]
    return request.app.state.templates.TemplateResponse(
        "tags.html",
        {"request": request, "tags": tags},
    )


@router.post("/{name}/approve")
async def approve_tag(request: Request, name: str):
    paperless = request.app.state.paperless
    try:
        entity = await paperless.create_tag(name)
        with get_conn() as conn:
            conn.execute(
                "UPDATE tag_whitelist SET approved = 1, paperless_id = ? WHERE name = ?",
                (entity.id, name),
            )
            conn.execute(
                """
                INSERT INTO audit_log (action, actor, details)
                VALUES ('tag_approve', 'user', ?)
                """,
                (f"Tag '{name}' approved and created with ID {entity.id}",),
            )
        log.info("tag approved", name=name, paperless_id=entity.id)
        return HTMLResponse(
            f'<tr id="tag-{name}" class="bg-green-50">'
            f'<td class="px-4 py-3 font-medium">{name}</td>'
            f'<td class="px-4 py-3">{entity.id}</td>'
            f'<td class="px-4 py-3"><span class="text-green-700">Approved</span></td>'
            f'<td class="px-4 py-3">—</td></tr>'
        )
    except Exception as exc:
        log.error("failed to approve tag", name=name, error=str(exc))
        return HTMLResponse(
            f'<div class="text-red-600 text-sm">Error: {exc}</div>',
            status_code=500,
        )


@router.post("/{name}/reject")
async def reject_tag(request: Request, name: str):
    with get_conn() as conn:
        conn.execute("DELETE FROM tag_whitelist WHERE name = ?", (name,))
        conn.execute(
            """
            INSERT INTO audit_log (action, actor, details)
            VALUES ('tag_reject', 'user', ?)
            """,
            (f"Tag '{name}' rejected and removed from whitelist",),
        )
    log.info("tag rejected", name=name)
    return HTMLResponse("")
