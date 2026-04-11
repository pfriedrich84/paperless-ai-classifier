"""Suggestion listing and (opt-in) approval/rejection tools."""

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

_SUGGESTION_COLS = (
    "id, document_id, created_at, status, confidence, reasoning, "
    "proposed_title, proposed_date, "
    "proposed_correspondent_name, proposed_correspondent_id, "
    "proposed_doctype_name, proposed_doctype_id, "
    "proposed_storage_path_name, proposed_storage_path_id, "
    "proposed_tags_json"
)


def _row_to_dict(row) -> dict:
    return {
        "suggestion_id": row["id"],
        "document_id": row["document_id"],
        "created_at": row["created_at"],
        "status": row["status"],
        "confidence": row["confidence"],
        "reasoning": row["reasoning"],
        "proposed_title": row["proposed_title"],
        "proposed_date": row["proposed_date"],
        "proposed_correspondent": {
            "name": row["proposed_correspondent_name"],
            "id": row["proposed_correspondent_id"],
        },
        "proposed_document_type": {
            "name": row["proposed_doctype_name"],
            "id": row["proposed_doctype_id"],
        },
        "proposed_storage_path": {
            "name": row["proposed_storage_path_name"],
            "id": row["proposed_storage_path_id"],
        },
        "proposed_tags": json.loads(row["proposed_tags_json"] or "[]"),
    }


def register(mcp: FastMCP) -> None:
    # ------------------------------------------------------------------
    # Always registered (read-only)
    # ------------------------------------------------------------------
    @mcp.tool(
        name="list_suggestions",
        description=(
            "List AI classification suggestions. "
            "Optionally filter by status: pending, accepted, rejected, committed, error."
        ),
        annotations=_RO,
    )
    async def list_suggestions(status: str | None = None, ctx: Context = None) -> str:
        check_api_key(ctx)
        with get_conn() as conn:
            if status:
                rows = conn.execute(
                    f"SELECT {_SUGGESTION_COLS} FROM suggestions "
                    "WHERE status = ? ORDER BY created_at DESC LIMIT 50",
                    (status,),
                ).fetchall()
            else:
                rows = conn.execute(
                    f"SELECT {_SUGGESTION_COLS} FROM suggestions ORDER BY created_at DESC LIMIT 50"
                ).fetchall()
        return json.dumps([_row_to_dict(r) for r in rows], ensure_ascii=False, default=str)

    @mcp.tool(
        name="get_suggestion",
        description="Get full details of a single classification suggestion.",
        annotations=_RO,
    )
    async def get_suggestion(suggestion_id: int, ctx: Context = None) -> str:
        check_api_key(ctx)
        with get_conn() as conn:
            row = conn.execute(
                f"SELECT {_SUGGESTION_COLS} FROM suggestions WHERE id = ?",
                (suggestion_id,),
            ).fetchone()
        if not row:
            return json.dumps({"error": f"Suggestion {suggestion_id} not found."})
        return json.dumps(_row_to_dict(row), ensure_ascii=False, default=str)

    # ------------------------------------------------------------------
    # Write tools — only registered when MCP_ENABLE_WRITE=true
    # ------------------------------------------------------------------
    if settings.mcp_enable_write:

        @mcp.tool(
            name="approve_suggestion",
            description=(
                "Accept a classification suggestion and commit its proposed "
                "metadata to Paperless-NGX. This writes changes to your documents."
            ),
            annotations=ToolAnnotations(
                readOnlyHint=False, destructiveHint=False, idempotentHint=True
            ),
        )
        async def approve_suggestion(suggestion_id: int, ctx: Context = None) -> str:
            check_api_key(ctx)
            deps = get_deps(ctx)

            from app.models import ReviewDecision, SuggestionRow
            from app.pipeline.committer import commit_suggestion

            with get_conn() as conn:
                row = conn.execute(
                    "SELECT * FROM suggestions WHERE id = ?", (suggestion_id,)
                ).fetchone()

            if not row:
                return json.dumps({"error": f"Suggestion {suggestion_id} not found."})
            if row["status"] != "pending":
                return json.dumps(
                    {"error": f"Suggestion is already '{row['status']}', not pending."}
                )

            suggestion = SuggestionRow(**dict(row))

            # Build tag IDs from proposed_tags_json
            tag_dicts = json.loads(suggestion.proposed_tags_json or "[]")
            tag_ids = [t["id"] for t in tag_dicts if t.get("id") is not None]

            decision = ReviewDecision(
                suggestion_id=suggestion.id,
                title=suggestion.proposed_title or "",
                date=suggestion.proposed_date,
                correspondent_id=suggestion.proposed_correspondent_id,
                doctype_id=suggestion.proposed_doctype_id,
                storage_path_id=suggestion.proposed_storage_path_id,
                tag_ids=tag_ids,
                action="accept",
            )

            await commit_suggestion(suggestion, decision, deps.paperless)

            # Audit log
            with get_conn() as conn:
                conn.execute(
                    """
                    INSERT INTO audit_log (action, document_id, actor, details)
                    VALUES ('mcp_approve', ?, 'mcp', ?)
                    """,
                    (suggestion.document_id, json.dumps({"suggestion_id": suggestion_id})),
                )

            log.info("suggestion approved via MCP", suggestion_id=suggestion_id)
            return json.dumps({"ok": True, "suggestion_id": suggestion_id, "action": "approved"})

        @mcp.tool(
            name="reject_suggestion",
            description="Reject a classification suggestion. The document stays unchanged.",
            annotations=ToolAnnotations(
                readOnlyHint=False, destructiveHint=False, idempotentHint=True
            ),
        )
        async def reject_suggestion(suggestion_id: int, ctx: Context = None) -> str:
            check_api_key(ctx)

            with get_conn() as conn:
                row = conn.execute(
                    "SELECT id, document_id, status FROM suggestions WHERE id = ?",
                    (suggestion_id,),
                ).fetchone()

                if not row:
                    return json.dumps({"error": f"Suggestion {suggestion_id} not found."})
                if row["status"] != "pending":
                    return json.dumps(
                        {"error": f"Suggestion is already '{row['status']}', not pending."}
                    )

                conn.execute(
                    "UPDATE suggestions SET status = 'rejected' WHERE id = ?",
                    (suggestion_id,),
                )
                conn.execute(
                    "UPDATE processed_documents SET status = 'rejected' WHERE document_id = ?",
                    (row["document_id"],),
                )
                conn.execute(
                    """
                    INSERT INTO audit_log (action, document_id, actor, details)
                    VALUES ('mcp_reject', ?, 'mcp', ?)
                    """,
                    (row["document_id"], json.dumps({"suggestion_id": suggestion_id})),
                )

            log.info("suggestion rejected via MCP", suggestion_id=suggestion_id)
            return json.dumps({"ok": True, "suggestion_id": suggestion_id, "action": "rejected"})
