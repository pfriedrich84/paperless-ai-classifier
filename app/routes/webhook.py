"""Optional webhook endpoint for Paperless post-consume hooks."""
from __future__ import annotations

import structlog
from fastapi import APIRouter, Request
from pydantic import BaseModel

from app.db import get_conn
from app.pipeline import classifier, context_builder
from app.pipeline.ocr_correction import maybe_correct_ocr
from app.worker import _process_document

log = structlog.get_logger(__name__)
router = APIRouter(prefix="/webhook")


class WebhookPayload(BaseModel):
    document_id: int


@router.post("/paperless")
async def paperless_webhook(request: Request, payload: WebhookPayload):
    """Process a single document triggered by a Paperless post-consume hook."""
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
            doc, paperless, ollama,
            correspondents, doctypes, storage_paths, tags,
        )
        return {"status": "ok", "document_id": doc_id}
    except Exception as exc:
        log.error("webhook processing failed", document_id=doc_id, error=str(exc))
        return {"status": "error", "document_id": doc_id, "error": str(exc)}
