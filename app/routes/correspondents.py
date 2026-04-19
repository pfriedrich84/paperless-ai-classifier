"""Correspondent whitelist and blacklist management routes."""

from __future__ import annotations

import structlog
from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse

from app.db import get_conn
from app.pipeline.committer import retroactive_correspondent_apply

log = structlog.get_logger(__name__)
router = APIRouter(prefix="/correspondents")


@router.get("")
async def correspondent_list(request: Request):
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT * FROM correspondent_whitelist ORDER BY approved ASC, times_seen DESC"
        ).fetchall()
        bl_rows = conn.execute(
            "SELECT * FROM correspondent_blacklist ORDER BY rejected_at DESC"
        ).fetchall()
    correspondents = [dict(r) for r in rows]
    blacklist = [dict(r) for r in bl_rows]
    return request.app.state.templates.TemplateResponse(
        request,
        "correspondents.html",
        {"correspondents": correspondents, "blacklist": blacklist},
    )


@router.post("/{name:path}/approve")
async def approve_correspondent(request: Request, name: str):
    paperless = request.app.state.paperless
    try:
        entity = await paperless.create_correspondent(name)
        with get_conn() as conn:
            conn.execute(
                "UPDATE correspondent_whitelist SET approved = 1, paperless_id = ? WHERE name = ?",
                (entity.id, name),
            )
            conn.execute(
                """
                INSERT INTO audit_log (action, actor, details)
                VALUES ('correspondent_approve', 'user', ?)
                """,
                (f"Correspondent '{name}' approved and created with ID {entity.id}",),
            )
        log.info("correspondent approved", name=name, paperless_id=entity.id)

        patched, pending = await retroactive_correspondent_apply(name, entity.id, paperless)

        retro_note = ""
        parts: list[str] = []
        if patched:
            parts.append(f"{patched} doc(s) updated")
        if pending:
            parts.append(f"{pending} pending resolved")
        if parts:
            retro_note = f' <span class="text-xs text-gray-500">({", ".join(parts)})</span>'

        return HTMLResponse(
            f'<tr id="corr-{name}" class="bg-green-50">'
            f'<td class="px-4 py-3 font-medium">{name}</td>'
            f'<td class="px-4 py-3">{entity.id}</td>'
            f'<td class="px-4 py-3"><span class="text-green-700">Approved</span>{retro_note}</td>'
            f'<td class="px-4 py-3">—</td></tr>'
        )
    except Exception as exc:
        log.error("failed to approve correspondent", name=name, error=str(exc))
        return HTMLResponse(
            f'<div class="text-red-600 text-sm">Error: {exc}</div>',
            status_code=500,
        )


@router.post("/{name:path}/reject")
async def reject_correspondent(request: Request, name: str):
    with get_conn() as conn:
        row = conn.execute(
            "SELECT times_seen FROM correspondent_whitelist WHERE name = ?", (name,)
        ).fetchone()
        times_seen = row["times_seen"] if row else 1
        conn.execute("DELETE FROM correspondent_whitelist WHERE name = ?", (name,))
        conn.execute(
            "INSERT OR REPLACE INTO correspondent_blacklist (name, times_seen) VALUES (?, ?)",
            (name, times_seen),
        )
        conn.execute(
            """
            INSERT INTO audit_log (action, actor, details)
            VALUES ('correspondent_blacklist', 'user', ?)
            """,
            (f"Correspondent '{name}' rejected and added to blacklist",),
        )
    log.info("correspondent blacklisted", name=name)
    return HTMLResponse("")


@router.post("/{name:path}/unblacklist")
async def unblacklist_correspondent(request: Request, name: str):
    with get_conn() as conn:
        conn.execute("DELETE FROM correspondent_blacklist WHERE name = ?", (name,))
        conn.execute(
            """
            INSERT INTO audit_log (action, actor, details)
            VALUES ('correspondent_unblacklist', 'user', ?)
            """,
            (f"Correspondent '{name}' removed from blacklist",),
        )
    log.info("correspondent unblacklisted", name=name)
    return HTMLResponse("")
