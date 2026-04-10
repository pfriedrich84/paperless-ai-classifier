"""Read-only tools for listing Paperless-NGX entities."""

from __future__ import annotations

import json

from mcp.server.fastmcp import Context, FastMCP
from mcp.types import ToolAnnotations

from app.mcp_tools._auth import check_api_key
from app.mcp_tools._deps import get_deps

_RO = ToolAnnotations(readOnlyHint=True, destructiveHint=False)


def _entity_to_dict(e) -> dict:
    return {"id": e.id, "name": e.name}


def register(mcp: FastMCP) -> None:
    @mcp.tool(
        name="list_correspondents",
        description="List all correspondents defined in Paperless-NGX.",
        annotations=_RO,
    )
    async def list_correspondents(ctx: Context) -> str:
        check_api_key(ctx)
        deps = get_deps(ctx)
        items = await deps.paperless.list_correspondents()
        return json.dumps([_entity_to_dict(e) for e in items], ensure_ascii=False)

    @mcp.tool(
        name="list_document_types",
        description="List all document types defined in Paperless-NGX.",
        annotations=_RO,
    )
    async def list_document_types(ctx: Context) -> str:
        check_api_key(ctx)
        deps = get_deps(ctx)
        items = await deps.paperless.list_document_types()
        return json.dumps([_entity_to_dict(e) for e in items], ensure_ascii=False)

    @mcp.tool(
        name="list_tags",
        description="List all tags defined in Paperless-NGX.",
        annotations=_RO,
    )
    async def list_tags(ctx: Context) -> str:
        check_api_key(ctx)
        deps = get_deps(ctx)
        items = await deps.paperless.list_tags()
        return json.dumps([_entity_to_dict(e) for e in items], ensure_ascii=False)

    @mcp.tool(
        name="list_storage_paths",
        description="List all storage paths defined in Paperless-NGX.",
        annotations=_RO,
    )
    async def list_storage_paths(ctx: Context) -> str:
        check_api_key(ctx)
        deps = get_deps(ctx)
        items = await deps.paperless.list_storage_paths()
        return json.dumps([_entity_to_dict(e) for e in items], ensure_ascii=False)
