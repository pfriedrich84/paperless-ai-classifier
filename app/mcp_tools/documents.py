"""Document search, retrieval, and (opt-in) update tools."""

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


def _doc_summary(d) -> dict:
    return {
        "id": d.id,
        "title": d.title,
        "created_date": d.created_date,
        "correspondent": d.correspondent,
        "document_type": d.document_type,
        "storage_path": d.storage_path,
        "tags": d.tags,
    }


def register(mcp: FastMCP) -> None:
    # ------------------------------------------------------------------
    # Always registered (read-only)
    # ------------------------------------------------------------------
    @mcp.tool(
        name="search_documents",
        description=(
            "Full-text search across all Paperless-NGX documents. "
            "Supports optional filters by tag names, correspondent, and document type."
        ),
        annotations=_RO,
    )
    async def search_documents(
        query: str,
        tags: list[str] | None = None,
        correspondent: str | None = None,
        document_type: str | None = None,
        ctx: Context = None,
    ) -> str:
        check_api_key(ctx)
        deps = get_deps(ctx)
        docs = await deps.paperless.search_documents(
            query=query, tags=tags, correspondent=correspondent, document_type=document_type
        )
        return json.dumps([_doc_summary(d) for d in docs], ensure_ascii=False, default=str)

    @mcp.tool(
        name="get_document",
        description=(
            "Get full details of a single document including its text content. "
            "Content is truncated to keep responses manageable."
        ),
        annotations=_RO,
    )
    async def get_document(document_id: int, ctx: Context = None) -> str:
        check_api_key(ctx)
        deps = get_deps(ctx)
        doc = await deps.paperless.get_document(document_id)
        content = doc.content or ""
        if len(content) > settings.max_doc_chars:
            content = content[: settings.max_doc_chars] + "\n...[truncated]"
        result = _doc_summary(doc)
        result["content"] = content
        return json.dumps(result, ensure_ascii=False, default=str)

    @mcp.tool(
        name="list_inbox",
        description="List all documents currently in the Paperless-NGX inbox (unprocessed).",
        annotations=_RO,
    )
    async def list_inbox(ctx: Context = None) -> str:
        check_api_key(ctx)
        deps = get_deps(ctx)
        docs = await deps.paperless.list_inbox_documents(settings.paperless_inbox_tag_id)
        return json.dumps([_doc_summary(d) for d in docs], ensure_ascii=False, default=str)

    # ------------------------------------------------------------------
    # Write tools — only registered when MCP_ENABLE_WRITE=true
    # ------------------------------------------------------------------
    if settings.mcp_enable_write:

        @mcp.tool(
            name="update_document",
            description=(
                "Update metadata of a Paperless-NGX document (title, correspondent, "
                "document type, storage path, tags). Only provided fields are changed."
            ),
            annotations=ToolAnnotations(
                readOnlyHint=False, destructiveHint=False, idempotentHint=True
            ),
        )
        async def update_document(
            document_id: int,
            title: str | None = None,
            correspondent_id: int | None = None,
            document_type_id: int | None = None,
            storage_path_id: int | None = None,
            tag_ids: list[int] | None = None,
            ctx: Context = None,
        ) -> str:
            check_api_key(ctx)
            deps = get_deps(ctx)
            fields: dict = {}
            if title is not None:
                fields["title"] = title
            if correspondent_id is not None:
                fields["correspondent"] = correspondent_id
            if document_type_id is not None:
                fields["document_type"] = document_type_id
            if storage_path_id is not None:
                fields["storage_path"] = storage_path_id
            if tag_ids is not None:
                fields["tags"] = tag_ids

            if not fields:
                return json.dumps({"error": "No fields to update."})

            await deps.paperless.patch_document(document_id, fields)

            with get_conn() as conn:
                conn.execute(
                    """
                    INSERT INTO audit_log (action, document_id, actor, details)
                    VALUES ('mcp_update', ?, 'mcp', ?)
                    """,
                    (document_id, json.dumps(fields, default=str, ensure_ascii=False)),
                )

            log.info("document updated via MCP", doc_id=document_id, fields=list(fields.keys()))
            return json.dumps(
                {"ok": True, "document_id": document_id, "updated_fields": list(fields.keys())}
            )
