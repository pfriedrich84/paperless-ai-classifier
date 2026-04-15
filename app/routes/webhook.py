"""Webhook endpoints for Paperless workflow and post-consume hooks."""

from __future__ import annotations

import json
import secrets

import structlog
from fastapi import APIRouter, Header, Request
from fastapi.responses import JSONResponse

from app.config import settings
from app.indexer import is_reindexing
from app.pipeline import context_builder
from app.pipeline.ocr_correction import (
    cache_ocr_correction,
    effective_ocr_mode,
    maybe_correct_ocr,
)
from app.worker import _process_document

log = structlog.get_logger(__name__)
router = APIRouter(prefix="/webhook")

# Max bytes of raw body to include in debug log (avoid flooding logs with PDF data)
_LOG_BODY_MAX = 2000


# ---------------------------------------------------------------------------
# Payload helpers
# ---------------------------------------------------------------------------
async def _parse_webhook_body(request: Request, endpoint: str) -> dict:
    """Parse the webhook request body, handling both JSON and multipart/form-data.

    Paperless-NGX can optionally attach the document file to webhook calls
    ("Dokument einbeziehen"), which sends the request as multipart/form-data
    instead of application/json.  The JSON payload is then in a form field.

    Logs Content-Type and payload for debugging.
    """
    content_type = request.headers.get("content-type", "")
    log.info(
        f"{endpoint} request received",
        content_type=content_type,
        method=request.method,
        url=str(request.url),
    )

    body: dict = {}

    if "multipart/form-data" in content_type:
        # Multipart: Paperless puts the payload in form fields alongside the file
        form = await request.form()
        form_keys = list(form.keys())
        log.info(f"{endpoint} multipart form fields", fields=form_keys)

        # Try to find the JSON payload in form fields
        # Paperless may send individual fields or a JSON blob
        for key in form_keys:
            value = form[key]
            # Skip file uploads (UploadFile objects)
            if hasattr(value, "read"):
                log.info(f"{endpoint} skipping file field", field=key)
                continue
            # Try to parse string values as JSON
            if isinstance(value, str):
                try:
                    parsed = json.loads(value)
                    if isinstance(parsed, dict):
                        body.update(parsed)
                        log.info(
                            f"{endpoint} parsed JSON from form field", field=key, payload=parsed
                        )
                        continue
                except (json.JSONDecodeError, ValueError):
                    pass
                # Store as plain key-value
                body[key] = value

        log.info(f"{endpoint} parsed multipart body", payload=body)
        # Clean up file handles
        await form.close()
    else:
        # JSON or other content types — read raw body for logging
        raw = await request.body()
        raw_preview = raw[:_LOG_BODY_MAX].decode("utf-8", errors="replace")
        if len(raw) > _LOG_BODY_MAX:
            raw_preview += f"... ({len(raw)} bytes total)"
        log.info(f"{endpoint} raw body", size=len(raw), preview=raw_preview)

        try:
            body = json.loads(raw)
        except (json.JSONDecodeError, ValueError) as exc:
            log.warning(f"{endpoint} JSON parse failed", error=str(exc))
            body = {}

    return body


def _extract_document_id(body: dict) -> int | None:
    """Extract document_id from various Paperless webhook payload formats.

    Supported formats:
      - Workflow webhook: ``{"event": "...", "object": {"id": 123, ...}}``
      - Post-consume:    ``{"document_id": 123}``
    """
    # Paperless workflow webhook format
    obj = body.get("object")
    if isinstance(obj, dict):
        raw = obj.get("id")
        if raw is not None:
            try:
                return int(raw)
            except (ValueError, TypeError):
                pass

    # Legacy post-consume format
    raw = body.get("document_id")
    if raw is not None:
        try:
            return int(raw)
        except (ValueError, TypeError):
            pass

    return None


def _verify_webhook_secret(secret_header: str | None) -> JSONResponse | None:
    """Return a 403 response if the webhook secret is configured and doesn't match."""
    if settings.webhook_secret and (
        not secret_header or not secrets.compare_digest(secret_header, settings.webhook_secret)
    ):
        return JSONResponse(status_code=403, content={"detail": "Invalid webhook secret"})
    return None


# ---------------------------------------------------------------------------
# /webhook/new — full processing (OCR + Embedding + Classification)
# ---------------------------------------------------------------------------
@router.post("/new")
async def webhook_new(
    request: Request,
    x_webhook_secret: str | None = Header(default=None),
):
    """Process a single document triggered by a Paperless webhook.

    Runs the full pipeline: OCR correction, embedding, classification,
    suggestion storage, and optional auto-commit / Telegram notification.

    Accepts both Paperless workflow webhook payloads
    (``{"event": "...", "object": {"id": ...}}``) and legacy post-consume
    payloads (``{"document_id": ...}``).
    """
    auth_error = _verify_webhook_secret(x_webhook_secret)
    if auth_error:
        log.warning("webhook auth failed")
        return auth_error

    body = await _parse_webhook_body(request, "webhook/new")
    doc_id = _extract_document_id(body)
    if doc_id is None:
        log.warning("webhook payload missing document id", payload=body)
        return JSONResponse(
            status_code=422,
            content={"detail": "Could not extract document_id from payload"},
        )

    if is_reindexing():
        log.info("reindex in progress — rejecting webhook", document_id=doc_id)
        return JSONResponse(
            status_code=503,
            content={"detail": "Reindex in progress, try again later"},
        )

    paperless = request.app.state.paperless
    ollama = request.app.state.ollama
    meili = request.app.state.meili

    log.info("webhook/new triggered", document_id=doc_id, webhook_event=body.get("event"))

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
            meili,
            correspondents,
            doctypes,
            storage_paths,
            tags,
        )
        return {"status": "ok", "document_id": doc_id}
    except Exception as exc:
        log.error("webhook/new processing failed", document_id=doc_id, error=str(exc))
        return {"status": "error", "document_id": doc_id, "error": str(exc)}


# ---------------------------------------------------------------------------
# /webhook/edit — embedding-only (no classification)
# ---------------------------------------------------------------------------
@router.post("/edit")
async def webhook_edit(
    request: Request,
    x_webhook_secret: str | None = Header(default=None),
):
    """Re-embed a single document triggered by a Paperless webhook.

    Only recomputes the document's embedding (with optional OCR correction).
    No classification, no suggestion, no Telegram notification.

    Use this when a document's content or metadata changed in Paperless and
    the embedding index should reflect the update.

    Accepts both Paperless workflow webhook payloads
    (``{"event": "...", "object": {"id": ...}}``) and legacy
    payloads (``{"document_id": ...}``).
    """
    auth_error = _verify_webhook_secret(x_webhook_secret)
    if auth_error:
        log.warning("webhook/edit auth failed")
        return auth_error

    body = await _parse_webhook_body(request, "webhook/edit")
    doc_id = _extract_document_id(body)
    if doc_id is None:
        log.warning("webhook/edit payload missing document id", payload=body)
        return JSONResponse(
            status_code=422,
            content={"detail": "Could not extract document_id from payload"},
        )

    if is_reindexing():
        log.info("reindex in progress — rejecting webhook/edit", document_id=doc_id)
        return JSONResponse(
            status_code=503,
            content={"detail": "Reindex in progress, try again later"},
        )

    paperless = request.app.state.paperless
    ollama = request.app.state.ollama
    meili = request.app.state.meili

    log.info("webhook/edit triggered", document_id=doc_id, webhook_event=body.get("event"))

    try:
        doc = await paperless.get_document(doc_id)

        # Optional OCR correction (caches locally, never writes to Paperless)
        ocr_mode = effective_ocr_mode()
        text, num_corrections = await maybe_correct_ocr(doc, ollama, paperless)
        if num_corrections > 0:
            doc = doc.model_copy(update={"content": text})
            cache_ocr_correction(doc.id, text, ocr_mode, num_corrections)

        # Compute and store embedding
        summary = context_builder.document_summary(doc)
        if not summary.strip():
            log.warning("empty document summary, skipping embedding", document_id=doc_id)
            return {"status": "ok", "document_id": doc_id, "action": "skipped_empty"}

        vec = await ollama.embed(summary)
        await context_builder.store_embedding(doc, vec, meili)

        log.info("document re-embedded", document_id=doc_id)
        return {"status": "ok", "document_id": doc_id, "action": "reembedded"}
    except Exception as exc:
        log.error("webhook/edit failed", document_id=doc_id, error=str(exc))
        return JSONResponse(
            status_code=500,
            content={"status": "error", "document_id": doc_id, "error": str(exc)},
        )
