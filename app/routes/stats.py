"""Statistics routes — counters and recent activity."""

from __future__ import annotations

from fastapi import APIRouter, Request

from app.db import get_conn

router = APIRouter(prefix="/stats")


@router.get("")
async def stats_page(request: Request):
    with get_conn() as conn:
        # Per-status counts
        status_counts = {}
        for row in conn.execute(
            "SELECT status, COUNT(*) AS c FROM suggestions GROUP BY status"
        ).fetchall():
            status_counts[row["status"]] = row["c"]

        # Last 7 days — committed per day
        daily_rows = conn.execute(
            """
            SELECT date(occurred_at) AS day, COUNT(*) AS c
            FROM audit_log
            WHERE action = 'commit' AND occurred_at >= date('now', '-7 days')
            GROUP BY day
            ORDER BY day
            """
        ).fetchall()
        daily = [{"day": r["day"], "count": r["c"]} for r in daily_rows]

        total_docs = conn.execute("SELECT COUNT(*) AS c FROM processed_documents").fetchone()["c"]

        total_errors = conn.execute("SELECT COUNT(*) AS c FROM errors").fetchone()["c"]

        embedded = conn.execute("SELECT COUNT(*) AS c FROM doc_embedding_meta").fetchone()["c"]

        # Confidence distribution in 5 buckets
        confidence_dist: dict[str, int] = {}
        for row in conn.execute(
            """
            SELECT
                CASE
                    WHEN confidence < 20 THEN '0-19'
                    WHEN confidence < 40 THEN '20-39'
                    WHEN confidence < 60 THEN '40-59'
                    WHEN confidence < 80 THEN '60-79'
                    ELSE '80-100'
                END AS bucket,
                COUNT(*) AS c
            FROM suggestions
            WHERE confidence IS NOT NULL
            GROUP BY bucket
            ORDER BY MIN(confidence)
            """
        ).fetchall():
            confidence_dist[row["bucket"]] = row["c"]

        unscored = conn.execute(
            "SELECT COUNT(*) AS c FROM suggestions WHERE confidence IS NULL"
        ).fetchone()["c"]

    return request.app.state.templates.TemplateResponse(
        "stats.html",
        {
            "request": request,
            "status_counts": status_counts,
            "daily": daily,
            "total_docs": total_docs,
            "total_errors": total_errors,
            "embedded": embedded,
            "confidence_dist": confidence_dist,
            "unscored": unscored,
        },
    )
