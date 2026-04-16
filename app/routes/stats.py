"""Statistics routes — counters, timing metrics, and recent activity."""

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

        # --- Phase duration averages (last 30 days, successful only) ---
        phase_avgs: dict[str, dict] = {}
        for row in conn.execute(
            """
            SELECT phase,
                   ROUND(AVG(duration_ms)) AS avg_ms,
                   MIN(duration_ms) AS min_ms,
                   MAX(duration_ms) AS max_ms,
                   COUNT(*) AS cnt
            FROM phase_timing
            WHERE success = 1 AND started_at >= datetime('now', '-30 days')
            GROUP BY phase
            """
        ).fetchall():
            phase_avgs[row["phase"]] = {
                "avg_ms": row["avg_ms"] or 0,
                "min_ms": row["min_ms"] or 0,
                "max_ms": row["max_ms"] or 0,
                "count": row["cnt"],
            }

        # --- Daily average durations (last 7 days) for trend chart ---
        daily_timing_rows = conn.execute(
            """
            SELECT date(started_at) AS day, phase,
                   ROUND(AVG(duration_ms)) AS avg_ms
            FROM phase_timing
            WHERE success = 1 AND started_at >= date('now', '-7 days')
            GROUP BY day, phase
            ORDER BY day
            """
        ).fetchall()
        daily_timing = [
            {"day": r["day"], "phase": r["phase"], "avg_ms": r["avg_ms"]} for r in daily_timing_rows
        ]

        # --- Average total pipeline time per document (last 30 days) ---
        avg_total_row = conn.execute(
            """
            SELECT ROUND(AVG(total_ms)) AS avg_ms, COUNT(*) AS cnt
            FROM (
                SELECT document_id, SUM(duration_ms) AS total_ms
                FROM phase_timing
                WHERE success = 1 AND started_at >= datetime('now', '-30 days')
                GROUP BY document_id
            )
            """
        ).fetchone()
        avg_total_ms = avg_total_row["avg_ms"] or 0 if avg_total_row else 0
        avg_total_count = avg_total_row["cnt"] or 0 if avg_total_row else 0

        # --- Error rate by phase (last 30 days) ---
        phase_errors: dict[str, dict] = {}
        for row in conn.execute(
            """
            SELECT phase,
                   COUNT(*) AS total,
                   SUM(CASE WHEN success = 0 THEN 1 ELSE 0 END) AS errors
            FROM phase_timing
            WHERE started_at >= datetime('now', '-30 days')
            GROUP BY phase
            """
        ).fetchall():
            total = row["total"]
            errors = row["errors"]
            phase_errors[row["phase"]] = {
                "total": total,
                "errors": errors,
                "rate_pct": round(errors / total * 100, 1) if total > 0 else 0,
            }

        # --- Auto-commit rate ---
        auto_row = conn.execute(
            """
            SELECT
                SUM(CASE WHEN actor = 'auto' THEN 1 ELSE 0 END) AS auto_count,
                COUNT(*) AS total_count
            FROM audit_log
            WHERE action = 'commit'
            """
        ).fetchone()
        auto_commits = auto_row["auto_count"] or 0 if auto_row else 0
        total_commits = auto_row["total_count"] or 0 if auto_row else 0

    return request.app.state.templates.TemplateResponse(
        request,
        "stats.html",
        {
            "status_counts": status_counts,
            "daily": daily,
            "total_docs": total_docs,
            "total_errors": total_errors,
            "embedded": embedded,
            "confidence_dist": confidence_dist,
            "unscored": unscored,
            "phase_avgs": phase_avgs,
            "daily_timing": daily_timing,
            "avg_total_ms": avg_total_ms,
            "avg_total_count": avg_total_count,
            "phase_errors": phase_errors,
            "auto_commits": auto_commits,
            "total_commits": total_commits,
        },
    )
