"""Optional webhook endpoint for Paperless post-consume hooks."""

from __future__ import annotations

import secrets

import structlog
from fastapi import APIRouter, Header, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from app.config import settings
from app.indexer import is_reindexing
from app.worker import _process_document

log = structlog.get_logger(__name__)
router = APIRouter(prefix="/webhook")


class WebhookPayload(BaseModel):
    document_id: int


@router.post("/paperless")
async def paperless_webhook(
    request: Request,
    payload: WebhookPayload,
    x_webhook_secret: str | None = Header(default=None),
):
    """Process a single document triggered by a Paperless post-consume hook."""
    # Verify webhook secret if configured
    if settings.webhook_secret and (
        not x_webhook_secret
        or not secrets.compare_digest(x_webhook_secret, settings.webhook_secret)
    ):
        log.warning("webhook auth failed", document_id=payload.document_id)
        return JSONResponse(status_code=403, content={"detail": "Invalid webhook secret"})

    if is_reindexing():
        log.info("reindex in progress — rejecting webhook", document_id=payload.document_id)
        return JSONResponse(
            status_code=503,
            content={"detail": "Reindex in progress, try again later"},
        )

    paperless = request.app.state.paperless
    ollama = request.app.state.ollama
    doc_id = payload.document_id

    log.info("webhook triggered", document_id=doc_id)

    try:
        doc = await paperless.get_document(doc_id)
        correspondents = await paperless.list_correspondents()
        doctypes = await paperless.list_document_types()
        storage_paths = await paperless.list_storage_paths()
        tags = await paperless.list_tags()

        await _process_document(
            doc,
            paperless,
            ollama,
            correspondents,
            doctypes,
            storage_paths,
            tags,
        )
        return {"status": "ok", "document_id": doc_id}
    except Exception as exc:
        log.error("webhook processing failed", document_id=doc_id, error=str(exc))
        return {"status": "error", "document_id": doc_id, "error": str(exc)}
