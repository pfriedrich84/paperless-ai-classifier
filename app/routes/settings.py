"""Settings routes — read-only config view and manual triggers."""

from __future__ import annotations

import structlog
from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse

from app.config import settings
from app.indexer import reindex_all
from app.worker import poll_inbox

log = structlog.get_logger(__name__)
router = APIRouter(prefix="/settings")


def _masked_config() -> list[tuple[str, str]]:
    """Return config key-value pairs with sensitive values masked."""
    items = []
    for field_name in settings.model_fields:
        value = getattr(settings, field_name)
        display = str(value)
        if "token" in field_name.lower() or "password" in field_name.lower():
            display = "***" if value else "(not set)"
        items.append((field_name, display))
    return items


@router.get("")
async def settings_page(request: Request):
    config_items = _masked_config()
    return request.app.state.templates.TemplateResponse(
        "settings.html",
        {"request": request, "config_items": config_items},
    )


@router.post("/trigger-poll")
async def trigger_poll(request: Request):
    log.info("manual poll triggered")
    try:
        await poll_inbox()
        return HTMLResponse(
            '<div class="text-green-700 text-sm font-medium mt-2">Poll completed successfully</div>'
        )
    except Exception as exc:
        log.error("manual poll failed", error=str(exc))
        return HTMLResponse(
            f'<div class="text-red-600 text-sm font-medium mt-2">Poll failed: {exc}</div>',
            status_code=500,
        )


@router.post("/trigger-reindex")
async def trigger_reindex(request: Request):
    log.info("manual reindex triggered")
    paperless = request.app.state.paperless
    ollama = request.app.state.ollama
    try:
        count = await reindex_all(paperless, ollama)
        return HTMLResponse(
            f'<div class="text-green-700 text-sm font-medium mt-2">'
            f"Reindex complete — {count} documents indexed</div>"
        )
    except Exception as exc:
        log.error("reindex failed", error=str(exc))
        return HTMLResponse(
            f'<div class="text-red-600 text-sm font-medium mt-2">Reindex failed: {exc}</div>',
            status_code=500,
        )
