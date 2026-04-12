"""Settings routes — config view, prompt editor, and manual triggers."""

from __future__ import annotations

import structlog
from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse

from app.config import settings
from app.db import get_conn
from app.indexer import reindex_all
from app.pipeline.classifier import _load_system_prompt, _prompt_override_path
from app.worker import poll_inbox

log = structlog.get_logger(__name__)
router = APIRouter(prefix="/settings")

MAX_PROMPT_SIZE = 50 * 1024  # 50 KB


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
    try:
        system_prompt = _load_system_prompt()
    except Exception:
        system_prompt = "(failed to load prompt)"
    is_custom = _prompt_override_path().is_file()
    return request.app.state.templates.TemplateResponse(
        "settings.html",
        {
            "request": request,
            "config_items": config_items,
            "system_prompt": system_prompt,
            "is_custom_prompt": is_custom,
        },
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


@router.post("/update-prompt")
async def update_prompt(request: Request, prompt_text: str = Form(...)):
    """Save a custom system prompt to the persistent data directory."""
    if len(prompt_text.encode("utf-8")) > MAX_PROMPT_SIZE:
        return HTMLResponse(
            '<div class="text-red-600 text-sm font-medium mt-2">'
            f"Prompt too large (max {MAX_PROMPT_SIZE // 1024} KB)</div>",
            status_code=400,
        )

    path = _prompt_override_path()
    try:
        path.write_text(prompt_text, encoding="utf-8")
        log.info("system prompt updated", path=str(path), size=len(prompt_text))
        with get_conn() as conn:
            conn.execute(
                "INSERT INTO audit_log (action, actor, details) VALUES (?, ?, ?)",
                ("prompt_update", "user", f"size={len(prompt_text)}"),
            )
    except Exception as exc:
        log.error("prompt save failed", error=str(exc))
        return HTMLResponse(
            f'<div class="text-red-600 text-sm font-medium mt-2">Save failed: {exc}</div>',
            status_code=500,
        )

    return HTMLResponse('<div class="text-green-700 text-sm font-medium mt-2">Prompt saved</div>')


@router.post("/reset-prompt")
async def reset_prompt(request: Request):
    """Delete the custom prompt override, reverting to the built-in default."""
    path = _prompt_override_path()
    try:
        path.unlink(missing_ok=True)
        log.info("system prompt reset to default")
        with get_conn() as conn:
            conn.execute(
                "INSERT INTO audit_log (action, actor, details) VALUES (?, ?, ?)",
                ("prompt_reset", "user", "reverted to built-in default"),
            )
    except Exception as exc:
        log.error("prompt reset failed", error=str(exc))
        return HTMLResponse(
            f'<div class="text-red-600 text-sm font-medium mt-2">Reset failed: {exc}</div>',
            status_code=500,
        )

    # Return the default prompt so the textarea updates
    default_prompt = (settings.prompts_dir / "classify_system.txt").read_text(encoding="utf-8")
    return HTMLResponse(
        f'<div class="text-green-700 text-sm font-medium mt-2">Reset to default</div>'
        f'<textarea id="prompt-text-area" name="prompt_text" rows="20"'
        f' class="mt-3 block w-full rounded-lg border-gray-300 shadow-sm'
        f" focus:border-primary-500 focus:ring-primary-500 text-sm font-mono"
        f' px-3 py-2 border">{default_prompt}</textarea>'
    )
