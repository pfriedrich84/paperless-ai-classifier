"""System status and health check tool."""

from __future__ import annotations

import json

from mcp.server.fastmcp import Context, FastMCP
from mcp.types import ToolAnnotations

from app.db import get_conn
from app.mcp_tools._auth import check_api_key
from app.mcp_tools._deps import get_deps

_RO = ToolAnnotations(readOnlyHint=True, destructiveHint=False)


def register(mcp: FastMCP) -> None:
    @mcp.tool(
        name="get_status",
        description=(
            "Health check: Paperless and Ollama connectivity, "
            "suggestion counts, recent errors, and embedded document count."
        ),
        annotations=_RO,
    )
    async def get_status(ctx: Context) -> str:
        check_api_key(ctx)
        deps = get_deps(ctx)

        paperless_ok = await deps.paperless.ping()
        ollama_ok = await deps.ollama.ping()

        with get_conn() as conn:
            pending = conn.execute(
                "SELECT COUNT(*) FROM suggestions WHERE status = 'pending'"
            ).fetchone()[0]
            committed = conn.execute(
                "SELECT COUNT(*) FROM suggestions WHERE status = 'committed'"
            ).fetchone()[0]
            errors = conn.execute(
                "SELECT COUNT(*) FROM errors WHERE occurred_at > datetime('now', '-24 hours')"
            ).fetchone()[0]
            embedded = conn.execute("SELECT COUNT(*) FROM doc_embedding_meta").fetchone()[0]
            pending_tags = conn.execute(
                "SELECT COUNT(*) FROM tag_whitelist WHERE approved = 0"
            ).fetchone()[0]

        return json.dumps(
            {
                "paperless_reachable": paperless_ok,
                "ollama_reachable": ollama_ok,
                "suggestions_pending": pending,
                "suggestions_committed": committed,
                "errors_last_24h": errors,
                "documents_embedded": embedded,
                "tags_pending_approval": pending_tags,
            }
        )
