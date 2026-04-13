"""AI classification tools — rate-limited, inbox-only."""

from __future__ import annotations

import json

import structlog
from mcp.server.fastmcp import Context, FastMCP
from mcp.types import ToolAnnotations

from app.config import settings
from app.mcp_tools._auth import check_api_key
from app.mcp_tools._deps import get_deps

log = structlog.get_logger(__name__)


def register(mcp: FastMCP) -> None:
    @mcp.tool(
        name="classify_document",
        description=(
            "Run the AI classification pipeline on an inbox document. "
            "Returns a classification suggestion with proposed title, date, "
            "correspondent, document type, tags, and confidence score. "
            "Only works on documents that carry the inbox tag. Rate-limited."
        ),
        annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=False),
    )
    async def classify_document(document_id: int, ctx: Context = None) -> str:
        check_api_key(ctx)
        deps = get_deps(ctx)

        # Rate limit
        deps.rate_limiter.check("classify")

        # Fetch document
        doc = await deps.paperless.get_document(document_id)

        # Inbox gate: only classify documents with the inbox tag
        if settings.paperless_inbox_tag_id not in doc.tags:
            return json.dumps(
                {
                    "error": (
                        f"Document {document_id} does not carry the inbox tag "
                        f"(tag ID {settings.paperless_inbox_tag_id}). "
                        "Only inbox documents can be classified via MCP."
                    )
                }
            )

        # Lazy imports to avoid circular dependencies at module load
        from app.pipeline import classifier, context_builder
        from app.pipeline.ocr_correction import maybe_correct_ocr
        from app.worker import _store_suggestion

        log.info("MCP classify_document", doc_id=document_id)

        # Optional OCR correction
        text, num_corrections = await maybe_correct_ocr(doc, deps.ollama, deps.paperless)
        if num_corrections > 0:
            doc = doc.model_copy(update={"content": text})

        # Find similar documents for context
        context_docs = await context_builder.find_similar_documents(
            doc, deps.paperless, deps.ollama
        )

        # Fetch entity lists
        correspondents = await deps.paperless.list_correspondents()
        doctypes = await deps.paperless.list_document_types()
        storage_paths = await deps.paperless.list_storage_paths()
        tags = await deps.paperless.list_tags()

        # Run classification
        result, raw_response = await classifier.classify(
            doc, context_docs, correspondents, doctypes, storage_paths, tags, deps.ollama
        )

        # Store suggestion in DB
        suggestion = _store_suggestion(
            doc, result, raw_response, correspondents, doctypes, storage_paths, tags
        )

        # Index for future context
        await context_builder.index_document(doc, deps.ollama)

        log.info(
            "MCP classification complete",
            doc_id=document_id,
            suggestion_id=suggestion.id,
            confidence=result.confidence,
        )

        return json.dumps(
            {
                "suggestion_id": suggestion.id,
                "document_id": document_id,
                "proposed_title": result.title,
                "proposed_date": result.date,
                "proposed_correspondent": result.correspondent,
                "proposed_document_type": result.document_type,
                "proposed_storage_path": result.storage_path,
                "proposed_tags": [
                    {"name": t.name, "confidence": t.confidence} for t in result.tags
                ],
                "confidence": result.confidence,
                "reasoning": result.reasoning,
            },
            ensure_ascii=False,
            default=str,
        )

    @mcp.tool(
        name="find_similar_documents",
        description=(
            "Find documents similar to a given document using embedding-based "
            "similarity search. Returns the most similar already-classified documents."
        ),
        annotations=ToolAnnotations(readOnlyHint=True, destructiveHint=False),
    )
    async def find_similar_documents(document_id: int, limit: int = 5, ctx: Context = None) -> str:
        check_api_key(ctx)
        deps = get_deps(ctx)

        from app.pipeline import context_builder

        doc = await deps.paperless.get_document(document_id)
        similar = await context_builder.find_similar_documents(
            doc, deps.paperless, deps.ollama, limit=limit
        )

        results = []
        for d in similar:
            content_preview = (d.content or "")[:500]
            results.append(
                {
                    "id": d.id,
                    "title": d.title,
                    "created_date": d.created_date,
                    "correspondent": d.correspondent,
                    "document_type": d.document_type,
                    "tags": d.tags,
                    "content_preview": content_preview,
                }
            )

        return json.dumps(results, ensure_ascii=False, default=str)
