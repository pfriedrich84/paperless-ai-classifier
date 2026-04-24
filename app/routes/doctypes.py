"""Document type whitelist and blacklist management routes."""

from __future__ import annotations

import structlog
from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse

from app.db import get_conn
from app.pipeline.committer import retroactive_doctype_apply
from app.ui_safety import encode_path_segment, escape_html

log = structlog.get_logger(__name__)
router = APIRouter(prefix="/doctypes")


@router.get("")
async def doctype_list(request: Request):
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT * FROM doctype_whitelist ORDER BY approved ASC, times_seen DESC"
        ).fetchall()
        bl_rows = conn.execute(
            "SELECT * FROM doctype_blacklist ORDER BY rejected_at DESC"
        ).fetchall()
    doctypes = [dict(r) for r in rows]
    blacklist = [dict(r) for r in bl_rows]
    return request.app.state.templates.TemplateResponse(
        request,
        "doctypes.html",
        {"doctypes": doctypes, "blacklist": blacklist},
    )


@router.post("/{name:path}/approve")
async def approve_doctype(request: Request, name: str):
    paperless = request.app.state.paperless
    try:
        entity = await paperless.create_document_type(name)
        with get_conn() as conn:
            conn.execute(
                "UPDATE doctype_whitelist SET approved = 1, paperless_id = ? WHERE name = ?",
                (entity.id, name),
            )
            conn.execute(
                """
                INSERT INTO audit_log (action, actor, details)
                VALUES ('doctype_approve', 'user', ?)
                """,
                (f"Document type '{name}' approved and created with ID {entity.id}",),
            )
        log.info("doctype approved", name=name, paperless_id=entity.id)

        patched, pending = await retroactive_doctype_apply(name, entity.id, paperless)

        retro_note = ""
        parts: list[str] = []
        if patched:
            parts.append(f"{patched} doc(s) updated")
        if pending:
            parts.append(f"{pending} pending resolved")
        if parts:
            retro_note = f' <span class="text-xs text-gray-500">({", ".join(parts)})</span>'

        encoded_name = encode_path_segment(name)
        safe_name = escape_html(name)
        return HTMLResponse(
            f'<tr id="dt-{encoded_name}" class="bg-green-50">'
            f'<td class="px-4 py-3 font-medium">{safe_name}</td>'
            f'<td class="px-4 py-3">{entity.id}</td>'
            f'<td class="px-4 py-3"><span class="text-green-700">Approved</span>{retro_note}</td>'
            f'<td class="px-4 py-3">—</td></tr>'
        )
    except Exception as exc:
        log.error("failed to approve doctype", name=name, error=str(exc))
        return HTMLResponse(
            '<div class="text-red-600 text-sm">Document type approval failed.</div>',
            status_code=500,
        )


@router.post("/{name:path}/reject")
async def reject_doctype(request: Request, name: str):
    with get_conn() as conn:
        row = conn.execute(
            "SELECT times_seen FROM doctype_whitelist WHERE name = ?", (name,)
        ).fetchone()
        times_seen = row["times_seen"] if row else 1
        conn.execute("DELETE FROM doctype_whitelist WHERE name = ?", (name,))
        conn.execute(
            "INSERT OR REPLACE INTO doctype_blacklist (name, times_seen) VALUES (?, ?)",
            (name, times_seen),
        )
        conn.execute(
            """
            INSERT INTO audit_log (action, actor, details)
            VALUES ('doctype_blacklist', 'user', ?)
            """,
            (f"Document type '{name}' rejected and added to blacklist",),
        )
    log.info("doctype blacklisted", name=name)
    return HTMLResponse("")


@router.post("/{name:path}/unblacklist")
async def unblacklist_doctype(request: Request, name: str):
    with get_conn() as conn:
        conn.execute("DELETE FROM doctype_blacklist WHERE name = ?", (name,))
        conn.execute(
            """
            INSERT INTO audit_log (action, actor, details)
            VALUES ('doctype_unblacklist', 'user', ?)
            """,
            (f"Document type '{name}' removed from blacklist",),
        )
    log.info("doctype unblacklisted", name=name)
    return HTMLResponse("")
