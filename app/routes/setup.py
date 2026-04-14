"""Onboarding wizard routes — guided first-run setup with connection tests."""

from __future__ import annotations

from typing import Any

import structlog
from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse

from app.config import needs_setup, settings

log = structlog.get_logger(__name__)
router = APIRouter(prefix="/setup")


# ---------------------------------------------------------------------------
# Wizard page (GET)
# ---------------------------------------------------------------------------
def _prefill_from_settings() -> dict[str, str]:
    """Seed wizard values from current settings (loaded from env / config.env)."""
    prefill: dict[str, str] = {}
    fields = {
        "paperless_url": str,
        "paperless_token": str,
        "paperless_inbox_tag_id": str,
        "ollama_url": str,
        "ollama_model": str,
        "enable_telegram": str,
        "telegram_bot_token": str,
        "telegram_chat_id": str,
    }
    for key, conv in fields.items():
        val = getattr(settings, key, None)
        if val is not None:
            s = conv(val)
            # Skip obvious placeholder defaults
            if s and s != "0":
                prefill[key] = s
    return prefill


@router.get("")
async def setup_page(request: Request):
    return request.app.state.templates.TemplateResponse(
        request,
        "setup.html",
        {"step": 1, "values": _prefill_from_settings(), "needs_setup": needs_setup()},
    )


# ---------------------------------------------------------------------------
# Step navigation (POST — HTMX partial swap)
# ---------------------------------------------------------------------------
def _collect_values(form: dict[str, Any]) -> dict[str, str]:
    """Extract wizard field values from the form, ignoring internal keys."""
    skip = {"step"}
    return {k: v for k, v in form.items() if k not in skip and v}


@router.post("/step/{step_num}")
async def wizard_step(request: Request, step_num: int):
    form = dict(await request.form())
    values = _collect_values(form)
    return request.app.state.templates.TemplateResponse(
        request,
        "setup.html",
        {"step": step_num, "values": values, "needs_setup": needs_setup()},
        headers={"HX-Push-Url": "false"},
    )


# ---------------------------------------------------------------------------
# Connection test: Paperless
# ---------------------------------------------------------------------------
@router.post("/test-paperless")
async def test_paperless(
    request: Request,
    paperless_url: str = Form(""),
    paperless_token: str = Form(""),
):
    if not paperless_url or not paperless_token:
        return HTMLResponse(
            '<div class="text-red-600 text-sm font-medium mt-2">URL and Token are required.</div>'
        )

    from app.clients.paperless import PaperlessClient

    client = PaperlessClient(base_url=paperless_url, token=paperless_token)
    try:
        ok = await client.ping()
        if not ok:
            return HTMLResponse(
                '<div class="text-red-600 text-sm font-medium mt-2">'
                "Connection failed — check URL and token.</div>"
            )

        # Fetch tags for inbox tag selection
        tags = await client.list_tags()
        options = "".join(
            f'<option value="{t.id}">{t.name} (ID: {t.id})</option>'
            for t in sorted(tags, key=lambda t: t.name)
        )

        return HTMLResponse(
            '<div class="text-green-700 text-sm font-medium mt-2">'
            "Connected successfully!</div>"
            '<div class="mt-3">'
            '<label class="block text-sm font-medium text-gray-700 mb-1">Inbox Tag</label>'
            '<select name="paperless_inbox_tag_id" required'
            ' class="block w-full rounded-lg border-gray-300 shadow-sm'
            " focus:border-primary-500 focus:ring-primary-500 text-sm"
            ' px-3 py-2 border bg-white">'
            '<option value="">— Select inbox tag —</option>'
            f"{options}"
            "</select>"
            '<p class="text-xs text-gray-500 mt-1">'
            "Select the tag used as your inbox (e.g. Posteingang).</p>"
            "</div>"
        )
    except Exception as exc:
        log.warning("paperless test failed", error=str(exc))
        return HTMLResponse(
            f'<div class="text-red-600 text-sm font-medium mt-2">Error: {exc}</div>'
        )
    finally:
        await client.aclose()


# ---------------------------------------------------------------------------
# Connection test: Ollama
# ---------------------------------------------------------------------------
@router.post("/test-ollama")
async def test_ollama(
    request: Request,
    ollama_url: str = Form(""),
    ollama_model: str = Form(""),
):
    if not ollama_url:
        return HTMLResponse(
            '<div class="text-red-600 text-sm font-medium mt-2">URL is required.</div>'
        )

    from app.clients.ollama import OllamaClient

    client = OllamaClient(base_url=ollama_url, model=ollama_model)
    try:
        ok = await client.ping()
        if not ok:
            return HTMLResponse(
                '<div class="text-red-600 text-sm font-medium mt-2">'
                "Connection failed — is Ollama running?</div>"
            )

        # Fetch available models for selection
        r = await client._client.get("/api/tags")
        r.raise_for_status()
        models = [m.get("name", "") for m in r.json().get("models", [])]

        if models:
            options = "".join(
                f'<option value="{m}" {"selected" if m == ollama_model or m.startswith(ollama_model + ":") else ""}>'
                f"{m}</option>"
                for m in sorted(models)
            )
            model_html = (
                '<div class="mt-3">'
                '<label class="block text-sm font-medium text-gray-700 mb-1">Select Model</label>'
                '<select name="ollama_model"'
                ' class="block w-full rounded-lg border-gray-300 shadow-sm'
                " focus:border-primary-500 focus:ring-primary-500 text-sm"
                ' px-3 py-2 border bg-white">'
                f"{options}"
                "</select>"
                "</div>"
            )
        else:
            model_html = (
                '<div class="mt-2 text-amber-600 text-sm">'
                "No models found. Pull a model first: "
                "<code>ollama pull gemma4:e2b</code></div>"
            )

        return HTMLResponse(
            '<div class="text-green-700 text-sm font-medium mt-2">'
            f"Connected! {len(models)} model(s) available.</div>"
            f"{model_html}"
        )
    except Exception as exc:
        log.warning("ollama test failed", error=str(exc))
        return HTMLResponse(
            f'<div class="text-red-600 text-sm font-medium mt-2">Error: {exc}</div>'
        )
    finally:
        await client.aclose()


# ---------------------------------------------------------------------------
# Connection test: Telegram
# ---------------------------------------------------------------------------
@router.post("/test-telegram")
async def test_telegram(
    request: Request,
    telegram_bot_token: str = Form(""),
    telegram_chat_id: str = Form(""),
):
    if not telegram_bot_token or not telegram_chat_id:
        return HTMLResponse(
            '<div class="text-red-600 text-sm font-medium mt-2">'
            "Bot Token and Chat ID are required.</div>"
        )

    from app.clients.telegram import TelegramClient

    # Create a client with telegram enabled temporarily
    client = TelegramClient(token=telegram_bot_token, chat_id=telegram_chat_id)
    try:
        payload = {
            "chat_id": telegram_chat_id,
            "text": "Test from Paperless AI Classifier setup",
            "parse_mode": "HTML",
        }
        r = await client._client.post(
            f"https://api.telegram.org/bot{telegram_bot_token}/sendMessage",
            data=payload,
        )
        r.raise_for_status()
        return HTMLResponse(
            '<div class="text-green-700 text-sm font-medium mt-2">'
            "Test message sent! Check your Telegram.</div>"
        )
    except Exception as exc:
        log.warning("telegram test failed", error=str(exc))
        return HTMLResponse(
            f'<div class="text-red-600 text-sm font-medium mt-2">Error: {exc}</div>'
        )
    finally:
        await client.aclose()


# ---------------------------------------------------------------------------
# Complete setup
# ---------------------------------------------------------------------------
@router.post("/complete")
async def complete_setup(request: Request):
    form = dict(await request.form())
    values = _collect_values(form)

    # Build the config dict with proper keys
    config: dict[str, Any] = {}
    for key in (
        "paperless_url",
        "paperless_token",
        "paperless_inbox_tag_id",
        "ollama_url",
        "ollama_model",
        "enable_telegram",
        "telegram_bot_token",
        "telegram_chat_id",
    ):
        if key in values:
            config[key] = values[key]

    # Validate required fields
    if not config.get("paperless_url") or not config.get("paperless_token"):
        return HTMLResponse(
            '<div class="text-red-600 text-sm font-medium mt-2">'
            "Paperless URL and Token are required.</div>",
            status_code=400,
        )

    inbox_tag = config.get("paperless_inbox_tag_id")
    if not inbox_tag or str(inbox_tag) == "0":
        return HTMLResponse(
            '<div class="text-red-600 text-sm font-medium mt-2">Please select an inbox tag.</div>',
            status_code=400,
        )

    # Convert types
    config["paperless_inbox_tag_id"] = int(config["paperless_inbox_tag_id"])
    if "enable_telegram" in config:
        config["enable_telegram"] = config["enable_telegram"].lower() in ("true", "1", "on")

    # Save config
    from app.config_writer import save_config

    changed, _restart = save_config(config)
    log.info("setup complete", saved_fields=list(changed.keys()))

    # Create clients and start services
    from app.clients.ollama import OllamaClient
    from app.clients.paperless import PaperlessClient
    from app.clients.telegram import TelegramClient
    from app.telegram_handler import start_telegram
    from app.worker import start_scheduler

    app = request.app

    # Close any existing clients
    for attr in ("paperless", "ollama", "meili", "telegram"):
        old = getattr(app.state, attr, None)
        if old and hasattr(old, "aclose"):
            await old.aclose()

    from app.clients.meilisearch import MeiliClient
    from app.db import EMBED_DIM

    paperless = PaperlessClient()
    ollama = OllamaClient()
    meili = MeiliClient()
    telegram = TelegramClient()
    app.state.paperless = paperless
    app.state.ollama = ollama
    app.state.meili = meili
    app.state.telegram = telegram

    if await meili.ping():
        await meili.ensure_index(EMBED_DIM)

    start_scheduler(app)
    start_telegram(telegram, paperless, ollama, meili)

    return HTMLResponse(
        "",
        status_code=200,
        headers={"HX-Redirect": "/"},
    )
