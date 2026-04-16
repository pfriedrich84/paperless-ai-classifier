"""Dashboard route — landing page with KPI counters + pipeline status."""

from __future__ import annotations

from datetime import UTC, datetime

import structlog
from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse

from app.config import settings
from app.db import get_conn
from app.worker import cancel_poll, get_poll_progress, start_poll_task

log = structlog.get_logger(__name__)
router = APIRouter()


def _last_poll(conn) -> dict | None:
    """Return the most recent completed poll cycle, or None."""
    row = conn.execute(
        """SELECT started_at, finished_at, total_docs, succeeded, failed, skipped
           FROM poll_cycles WHERE finished_at IS NOT NULL
           ORDER BY finished_at DESC LIMIT 1"""
    ).fetchone()
    return dict(row) if row else None


def _next_run_iso(request: Request) -> str | None:
    """Return the next scheduled poll time as ISO string, or None."""
    scheduler = getattr(request.app.state, "scheduler", None)
    if not scheduler:
        return None
    job = scheduler.get_job("poll_inbox")
    if job and job.next_run_time:
        return job.next_run_time.isoformat()
    return None


def _render_pipeline_status(poll, last_poll: dict | None, next_run: str | None) -> str:
    """Build HTML fragment for the pipeline status card."""
    now = datetime.now(tz=UTC)

    if poll.running:
        # --- Running state ---
        phase_labels = {
            "prepare": "Preparing...",
            "ocr": "OCR correction...",
            "embed": "Embedding...",
            "classify": "Classifying...",
        }
        label = phase_labels.get(poll.phase, poll.phase or "Starting...")
        pct = int(poll.done / poll.total * 100) if poll.total > 0 else 0
        elapsed = ""
        if poll.started_at:
            try:
                started = datetime.fromisoformat(poll.started_at)
                secs = int((now - started).total_seconds())
                elapsed = f' <span class="text-gray-400">({secs}s elapsed)</span>'
            except (ValueError, TypeError):
                pass

        return (
            f'<div id="pipeline-status" hx-get="/pipeline-status"'
            f' hx-trigger="every 3s" hx-swap="outerHTML"'
            f' class="mb-6 bg-white rounded-xl shadow-sm border border-blue-200 p-6">'
            f'<div class="flex items-center justify-between mb-3">'
            f'<div class="flex items-center gap-3">'
            f'<span class="relative flex h-3 w-3">'
            f'<span class="animate-ping absolute inline-flex h-full w-full rounded-full bg-blue-400 opacity-75"></span>'
            f'<span class="relative inline-flex rounded-full h-3 w-3 bg-blue-500"></span>'
            f"</span>"
            f'<span class="text-sm font-semibold text-blue-700">Pipeline Running</span>'
            f'<span class="text-sm text-gray-500">{label}{elapsed}</span>'
            f"</div>"
            f'<button hx-post="/cancel-poll-dashboard" hx-target="#pipeline-status" hx-swap="outerHTML"'
            f' class="text-xs px-3 py-1 rounded bg-gray-100 text-gray-600 hover:bg-gray-200">Cancel</button>'
            f"</div>"
            f'<div class="w-full bg-gray-100 rounded-full h-2.5">'
            f'<div class="bg-blue-500 h-2.5 rounded-full transition-all" style="width:{pct}%"></div>'
            f"</div>"
            f'<p class="text-xs text-gray-400 mt-1">{poll.done}/{poll.total} documents</p>'
            f"</div>"
        )

    # --- Idle state ---
    last_info = ""
    if last_poll:
        try:
            finished = datetime.fromisoformat(last_poll["finished_at"] + "+00:00")
            ago_secs = int((now - finished).total_seconds())
            if ago_secs < 60:
                ago = f"{ago_secs}s ago"
            elif ago_secs < 3600:
                ago = f"{ago_secs // 60}m ago"
            else:
                ago = f"{ago_secs // 3600}h {(ago_secs % 3600) // 60}m ago"
            total = last_poll["total_docs"]
            ok = last_poll["succeeded"]
            fail = last_poll["failed"]
            fail_txt = f', <span class="text-red-500">{fail} failed</span>' if fail else ""
            last_info = (
                f'<span class="text-sm text-gray-500">Last poll: {ago}'
                f" &mdash; {total} docs ({ok} succeeded{fail_txt})</span>"
            )
        except (ValueError, TypeError):
            pass

    next_info = ""
    if next_run:
        try:
            next_dt = datetime.fromisoformat(next_run)
            remaining = int((next_dt - now).total_seconds())
            if remaining > 0:
                mins, secs = divmod(remaining, 60)
                next_info = (
                    f'<span class="text-sm text-gray-400">Next poll in {mins}m {secs}s</span>'
                )
        except (ValueError, TypeError):
            pass

    if not next_info:
        interval = settings.poll_interval_seconds
        if interval <= 0:
            next_info = '<span class="text-sm text-gray-400">Automatic polling disabled</span>'
        else:
            next_info = f'<span class="text-sm text-gray-400">Interval: every {interval}s</span>'

    return (
        f'<div id="pipeline-status" hx-get="/pipeline-status"'
        f' hx-trigger="every 30s" hx-swap="outerHTML"'
        f' class="mb-6 bg-white rounded-xl shadow-sm border border-gray-200 p-6">'
        f'<div class="flex items-center justify-between flex-wrap gap-2">'
        f'<div class="flex items-center gap-3">'
        f'<span class="inline-flex rounded-full h-3 w-3 bg-green-500"></span>'
        f'<span class="text-sm font-semibold text-green-700">Pipeline Idle</span>'
        f"{last_info}"
        f"</div>"
        f'<div class="flex items-center gap-3">'
        f"{next_info}"
        f'<button hx-post="/trigger-poll-dashboard" hx-target="#pipeline-status" hx-swap="outerHTML"'
        f' hx-disabled-elt="this"'
        f' class="text-xs px-3 py-1.5 rounded-md bg-primary-600 text-white hover:bg-primary-700'
        f' disabled:opacity-50">Run Now</button>'
        f"</div>"
        f"</div>"
        f"</div>"
    )


@router.get("/pipeline-status")
async def pipeline_status(request: Request):
    """HTMX partial: pipeline status card content."""
    poll = get_poll_progress()
    with get_conn() as conn:
        last_poll = _last_poll(conn)
    next_run = _next_run_iso(request)
    return HTMLResponse(_render_pipeline_status(poll, last_poll, next_run))


@router.post("/trigger-poll-dashboard")
async def trigger_poll_dashboard(request: Request):
    """Start a manual poll and return an updated pipeline status card."""
    start_poll_task()
    poll = get_poll_progress()
    with get_conn() as conn:
        last_poll = _last_poll(conn)
    next_run = _next_run_iso(request)
    return HTMLResponse(_render_pipeline_status(poll, last_poll, next_run))


@router.post("/cancel-poll-dashboard")
async def cancel_poll_dashboard(request: Request):
    """Cancel a running poll and return an updated pipeline status card."""
    cancel_poll()
    poll = get_poll_progress()
    with get_conn() as conn:
        last_poll = _last_poll(conn)
    next_run = _next_run_iso(request)
    return HTMLResponse(_render_pipeline_status(poll, last_poll, next_run))


@router.get("/")
async def dashboard(request: Request):
    with get_conn() as conn:
        pending = conn.execute(
            "SELECT COUNT(DISTINCT document_id) AS c FROM suggestions WHERE status = 'pending'"
        ).fetchone()["c"]

        committed_today = conn.execute(
            """
            SELECT COUNT(DISTINCT document_id) AS c FROM audit_log
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
        request,
        "index.html",
        {
            "pending": pending,
            "committed_today": committed_today,
            "error_count": error_count,
            "pending_tags": pending_tags,
        },
    )
