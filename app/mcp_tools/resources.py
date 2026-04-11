"""MCP resources providing read-only summaries.

Note: FastMCP resources cannot receive a Context parameter, so resources
that need async client access (e.g. Paperless API) are not feasible here.
Use the corresponding tools (list_inbox, list_suggestions) instead for
live data.  Resources here provide DB-only snapshots.
"""

from __future__ import annotations

import json

from mcp.server.fastmcp import FastMCP

from app.db import get_conn


def register(mcp: FastMCP) -> None:
    @mcp.resource(
        uri="paperless://suggestions/pending",
        name="Pending Suggestions",
        description="AI classification suggestions awaiting review (from local DB).",
    )
    async def pending_suggestions_resource() -> str:
        with get_conn() as conn:
            rows = conn.execute(
                """
                SELECT id, document_id, proposed_title, confidence, created_at
                FROM suggestions
                WHERE status = 'pending'
                ORDER BY created_at DESC
                LIMIT 50
                """
            ).fetchall()

        items = [
            {
                "suggestion_id": r["id"],
                "document_id": r["document_id"],
                "proposed_title": r["proposed_title"],
                "confidence": r["confidence"],
                "created_at": r["created_at"],
            }
            for r in rows
        ]
        return json.dumps({"count": len(items), "suggestions": items}, ensure_ascii=False)

    @mcp.resource(
        uri="paperless://stats",
        name="Classifier Stats",
        description="Quick stats from the local classifier database.",
    )
    async def stats_resource() -> str:
        with get_conn() as conn:
            pending = conn.execute(
                "SELECT COUNT(*) FROM suggestions WHERE status = 'pending'"
            ).fetchone()[0]
            committed = conn.execute(
                "SELECT COUNT(*) FROM suggestions WHERE status = 'committed'"
            ).fetchone()[0]
            rejected = conn.execute(
                "SELECT COUNT(*) FROM suggestions WHERE status = 'rejected'"
            ).fetchone()[0]
            errors = conn.execute(
                "SELECT COUNT(*) FROM errors WHERE occurred_at > datetime('now', '-24 hours')"
            ).fetchone()[0]
            pending_tags = conn.execute(
                "SELECT COUNT(*) FROM tag_whitelist WHERE approved = 0"
            ).fetchone()[0]

        return json.dumps(
            {
                "suggestions_pending": pending,
                "suggestions_committed": committed,
                "suggestions_rejected": rejected,
                "errors_last_24h": errors,
                "tags_pending_approval": pending_tags,
            }
        )
