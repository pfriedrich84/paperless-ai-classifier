"""Settings routes — config view, prompt editor, and manual triggers."""

from __future__ import annotations

from collections import OrderedDict
from typing import Any

import structlog
from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse

from app.config import FIELD_META, needs_setup, settings
from app.db import get_conn
from app.indexer import get_reindex_progress, start_reindex_task
from app.pipeline.classifier import _load_system_prompt, _prompt_override_path
from app.worker import poll_inbox

log = structlog.get_logger(__name__)
router = APIRouter(prefix="/settings")

MAX_PROMPT_SIZE = 50 * 1024  # 50 KB


def _build_config_groups() -> OrderedDict[str, list[tuple[str, dict[str, Any], Any]]]:
    """Group settings fields by category for the editable form."""
    groups: OrderedDict[str, list[tuple[str, dict[str, Any], Any]]] = OrderedDict()
    for field_name, meta in FIELD_META.items():
        cat = meta["category"]
        value = getattr(settings, field_name, "")
        if cat not in groups:
            groups[cat] = []
        groups[cat].append((field_name, meta, value))
    return groups


@router.get("")
async def settings_page(request: Request):
    config_groups = _build_config_groups()
    try:
        system_prompt = _load_system_prompt()
    except Exception:
        system_prompt = "(failed to load prompt)"
    is_custom = _prompt_override_path().is_file()
    progress = get_reindex_progress()
    return request.app.state.templates.TemplateResponse(
        request,
        "settings.html",
        {
            "config_groups": config_groups,
            "system_prompt": system_prompt,
            "is_custom_prompt": is_custom,
            "reindex": progress,
            "needs_setup": needs_setup(),
        },
    )


@router.post("/save-config")
async def save_config_route(request: Request):
    """Save configuration changes from the settings form."""
    from app.config_writer import apply_runtime_changes, save_config

    form = dict(await request.form())

    # Build updates dict from form fields (only known settings fields)
    updates: dict[str, Any] = {}
    for field_name in FIELD_META:
        if field_name in form:
            updates[field_name] = form[field_name]
        elif FIELD_META[field_name]["input_type"] == "bool":
            # Unchecked checkboxes are not submitted — treat as false
            updates[field_name] = "false"

    if not updates:
        return HTMLResponse(
            '<div class="text-gray-500 text-sm font-medium mt-2">No changes detected.</div>'
        )

    changed, restart_required = save_config(updates)

    if not changed:
        return HTMLResponse(
            '<div class="text-gray-500 text-sm font-medium mt-2">No changes detected.</div>'
        )

    # Apply runtime changes for non-restart fields
    runtime_fields = {k: v for k, v in changed.items() if k not in restart_required}
    actions: list[str] = []
    if runtime_fields:
        actions = await apply_runtime_changes(request.app, changed)

    parts = []
    applied = [k for k in changed if k not in restart_required]
    if applied:
        parts.append(f"Applied: {', '.join(applied)}")
    if restart_required:
        parts.append(f"Requires restart: {', '.join(restart_required)}")
    if actions:
        parts.append(f"Actions: {', '.join(actions)}")

    msg = ". ".join(parts) + "." if parts else "Saved."
    return HTMLResponse(
        f'<div class="text-green-700 text-sm font-medium mt-2">Saved. {msg}</div>'
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

    started = start_reindex_task(paperless, ollama)
    if not started:
        return HTMLResponse(
            '<div class="text-amber-600 text-sm font-medium mt-2">Reindex is already running</div>'
        )

    return HTMLResponse(_render_reindex_progress(get_reindex_progress()))


@router.get("/reindex-status")
async def reindex_status(request: Request):
    return HTMLResponse(_render_reindex_progress(get_reindex_progress()))


@router.get("/reindex-banner")
async def reindex_banner(request: Request):
    progress = get_reindex_progress()
    if not progress.running:
        return HTMLResponse("")
    return HTMLResponse(
        f'<div class="bg-amber-50 border-b border-amber-200 px-4 py-2'
        f' text-center text-sm text-amber-800">'
        f"Reindex in progress ({progress.done}/{progress.total} documents)"
        f" — inbox processing is paused</div>"
    )


def _render_reindex_progress(progress) -> str:
    """Build an HTML fragment for the reindex progress area."""
    if progress.running:
        pct = int(progress.done / progress.total * 100) if progress.total > 0 else 0
        return (
            '<div id="reindex-result" hx-get="/settings/reindex-status"'
            ' hx-trigger="every 2s" hx-swap="outerHTML">'
            '<div class="mt-2">'
            '<div class="flex justify-between text-sm text-gray-600 mb-1">'
            "<span>Reindexing…</span>"
            f"<span>{progress.done} / {progress.total} documents</span>"
            "</div>"
            '<div class="w-full bg-gray-200 rounded-full h-2.5">'
            '<div class="bg-primary-600 h-2.5 rounded-full transition-all duration-500"'
            f' style="width: {pct}%"></div>'
            "</div>"
            "</div></div>"
        )

    if progress.error:
        return (
            '<div id="reindex-result">'
            '<div class="text-red-600 text-sm font-medium mt-2">'
            f"Reindex failed: {progress.error}</div></div>"
        )

    if progress.finished_at:
        done = progress.done - progress.failed
        return (
            '<div id="reindex-result">'
            '<div class="text-green-700 text-sm font-medium mt-2">'
            f"Reindex complete — {done} documents indexed</div></div>"
        )

    return '<div id="reindex-result"></div>'


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
