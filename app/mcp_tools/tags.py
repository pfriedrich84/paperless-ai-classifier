"""Tag whitelist tools — listing and (opt-in) approval."""

from __future__ import annotations

import json

import structlog
from mcp.server.fastmcp import Context, FastMCP
from mcp.types import ToolAnnotations

from app.config import settings
from app.db import get_conn
from app.mcp_tools._auth import check_api_key
from app.mcp_tools._deps import get_deps

log = structlog.get_logger(__name__)

_RO = ToolAnnotations(readOnlyHint=True, destructiveHint=False)


def register(mcp: FastMCP) -> None:
    # ------------------------------------------------------------------
    # Always registered (read-only)
    # ------------------------------------------------------------------
    @mcp.tool(
        name="list_tag_proposals",
        description=(
            "List tags proposed by the AI that are not yet approved. "
            "These are new tags the LLM suggested but that don't exist in Paperless yet."
        ),
        annotations=_RO,
    )
    async def list_tag_proposals(ctx: Context = None) -> str:
        check_api_key(ctx)
        with get_conn() as conn:
            rows = conn.execute(
                "SELECT name, times_seen, first_seen, notes "
                "FROM tag_whitelist WHERE approved = 0 "
                "ORDER BY times_seen DESC"
            ).fetchall()

        items = [
            {
                "name": r["name"],
                "times_seen": r["times_seen"],
                "first_seen": r["first_seen"],
                "notes": r["notes"],
            }
            for r in rows
        ]
        return json.dumps(items, ensure_ascii=False)

    # ------------------------------------------------------------------
    # Write tools — only registered when MCP_ENABLE_WRITE=true
    # ------------------------------------------------------------------
    if settings.mcp_enable_write:

        @mcp.tool(
            name="approve_tag",
            description=(
                "Approve a proposed tag: creates it in Paperless-NGX and marks it "
                "as approved in the whitelist. Future classifications can then use it."
            ),
            annotations=ToolAnnotations(
                readOnlyHint=False, destructiveHint=False, idempotentHint=False
            ),
        )
        async def approve_tag(name: str, ctx: Context = None) -> str:
            check_api_key(ctx)
            deps = get_deps(ctx)

            # Check it exists in whitelist
            with get_conn() as conn:
                row = conn.execute(
                    "SELECT name, approved, paperless_id FROM tag_whitelist WHERE name = ?",
                    (name,),
                ).fetchone()

            if not row:
                return json.dumps({"error": f"Tag '{name}' not found in proposals."})
            if row["approved"]:
                return json.dumps(
                    {
                        "error": f"Tag '{name}' is already approved (Paperless ID: {row['paperless_id']})."
                    }
                )

            # Create in Paperless
            entity = await deps.paperless.create_tag(name)

            # Update whitelist
            with get_conn() as conn:
                conn.execute(
                    "UPDATE tag_whitelist SET approved = 1, paperless_id = ? WHERE name = ?",
                    (entity.id, name),
                )
                conn.execute(
                    """
                    INSERT INTO audit_log (action, document_id, actor, details)
                    VALUES ('mcp_approve_tag', NULL, 'mcp', ?)
                    """,
                    (json.dumps({"tag_name": name, "paperless_id": entity.id}),),
                )

            log.info("tag approved via MCP", tag_name=name, paperless_id=entity.id)
            return json.dumps({"ok": True, "tag_name": name, "paperless_id": entity.id})
