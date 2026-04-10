"""Dashboard route — landing page with KPI counters."""

from __future__ import annotations

import structlog
from fastapi import APIRouter, Request

from app.db import get_conn

log = structlog.get_logger(__name__)
router = APIRouter()


@router.get("/")
async def dashboard(request: Request):
    with get_conn() as conn:
        pending = conn.execute(
            "SELECT COUNT(*) AS c FROM suggestions WHERE status = 'pending'"
        ).fetchone()["c"]

        committed_today = conn.execute(
            """
            SELECT COUNT(*) AS c FROM audit_log
            WHERE action = 'commit' AND occurred_at >= date('now')
            """
        ).fetchone()["c"]

        error_count = conn.execute(
            """
            SELECT COUNT(*) AS c FROM errors
            WHERE occurred_at >= datetime('now', '-24 hours')
            """
        ).fetchone()["c"]

        pending_tags = conn.execute(
            "SELECT COUNT(*) AS c FROM tag_whitelist WHERE approved = 0"
        ).fetchone()["c"]

    return request.app.state.templates.TemplateResponse(
        "index.html",
        {
            "request": request,
            "pending": pending,
            "committed_today": committed_today,
            "error_count": error_count,
            "pending_tags": pending_tags,
        },
    )
