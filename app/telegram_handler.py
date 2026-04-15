"""Telegram bot: send suggestion notifications, handle callbacks, and RAG chat."""

from __future__ import annotations

import asyncio
import json

import structlog

from app.clients.ollama import OllamaClient
from app.clients.paperless import PaperlessClient
from app.clients.telegram import TelegramClient
from app.config import settings
from app.db import get_conn
from app.models import ReviewDecision, SuggestionRow
from app.pipeline.committer import commit_suggestion

log = structlog.get_logger(__name__)

_telegram: TelegramClient | None = None
_paperless: PaperlessClient | None = None
_ollama: OllamaClient | None = None
_poll_task: asyncio.Task | None = None  # type: ignore[type-arg]


# ----------------------------------------------------------------------
# Notification: send a suggestion to Telegram
# ----------------------------------------------------------------------
def _build_suggestion_message(suggestion: SuggestionRow) -> tuple[str, dict]:
    """Build the Telegram message text and inline keyboard for a suggestion."""
    conf = suggestion.confidence or 0
    conf_emoji = "\U0001f7e2" if conf >= 80 else ("\U0001f7e1" if conf >= 50 else "\U0001f534")
    display_title = suggestion.proposed_title or "\u2014"

    lines = [
        f"\U0001f4c4 <b>Document #{suggestion.document_id}</b>",
        "",
        f"<b>Title:</b> {display_title}",
    ]
    if suggestion.proposed_correspondent_name:
        lines.append(f"<b>Correspondent:</b> {suggestion.proposed_correspondent_name}")
    if suggestion.proposed_doctype_name:
        lines.append(f"<b>Type:</b> {suggestion.proposed_doctype_name}")
    if suggestion.proposed_date:
        lines.append(f"<b>Date:</b> {suggestion.proposed_date}")
    lines.append(f"\n{conf_emoji} <b>Confidence:</b> {conf}%")
    if suggestion.reasoning:
        lines.append(f"\n<i>{suggestion.reasoning}</i>")

    text = "\n".join(lines)

    # Build the review URL for "Edit in GUI" button
    gui_base = settings.gui_base_url or f"http://localhost:{settings.gui_port}"
    keyboard = {
        "inline_keyboard": [
            [
                {"text": "\u2705 Accept", "callback_data": f"accept:{suggestion.id}"},
                {"text": "\u274c Reject", "callback_data": f"reject:{suggestion.id}"},
            ],
            [
                {
                    "text": "\u270f\ufe0f Edit in GUI",
                    "url": f"{gui_base}/review/{suggestion.id}",
                },
            ],
        ],
    }

    return text, keyboard


async def notify_suggestion(suggestion: SuggestionRow) -> None:
    """Send a Telegram notification for a new suggestion."""
    if not _telegram or not _telegram.enabled:
        return
    text, keyboard = _build_suggestion_message(suggestion)
    await _telegram.send_message(text, reply_markup=keyboard)
    log.info("telegram notification sent", suggestion_id=suggestion.id)


# ----------------------------------------------------------------------
# Callback handler: process Accept / Reject button presses
# ----------------------------------------------------------------------
async def _handle_callback(update: dict) -> None:
    """Process a callback_query from an inline keyboard button."""
    cb = update.get("callback_query")
    if not cb:
        return

    data = cb.get("data", "")
    cb_id = cb["id"]
    message = cb.get("message", {})
    chat_id = message.get("chat", {}).get("id")
    message_id = message.get("message_id")

    if ":" not in data:
        await _telegram.answer_callback_query(cb_id, "Unknown action")
        return

    action, suggestion_id_str = data.split(":", 1)
    try:
        suggestion_id = int(suggestion_id_str)
    except ValueError:
        await _telegram.answer_callback_query(cb_id, "Invalid ID")
        return

    # Load suggestion from DB
    with get_conn() as conn:
        row = conn.execute("SELECT * FROM suggestions WHERE id = ?", (suggestion_id,)).fetchone()

    if not row:
        await _telegram.answer_callback_query(cb_id, "Suggestion not found")
        return

    suggestion = SuggestionRow(**dict(row))

    if suggestion.status != "pending":
        await _telegram.answer_callback_query(cb_id, f"Already {suggestion.status}")
        if _telegram and chat_id and message_id:
            await _telegram.edit_message_text(
                chat_id,
                message_id,
                f"\u2139\ufe0f Suggestion #{suggestion_id} already <b>{suggestion.status}</b>.",
            )
        return

    if action == "accept":
        await _accept_via_telegram(suggestion, cb_id, chat_id, message_id)
    elif action == "reject":
        await _reject_via_telegram(suggestion, cb_id, chat_id, message_id)
    else:
        await _telegram.answer_callback_query(cb_id, "Unknown action")


async def _accept_via_telegram(
    suggestion: SuggestionRow, cb_id: str, chat_id: int | str, message_id: int
) -> None:
    """Accept a suggestion using its proposed values."""
    # Resolve proposed tag IDs from stored JSON
    tag_ids: list[int] = []
    if suggestion.proposed_tags_json:
        try:
            for t in json.loads(suggestion.proposed_tags_json):
                tid = t.get("id")
                if tid is not None:
                    tag_ids.append(tid)
        except json.JSONDecodeError:
            pass

    decision = ReviewDecision(
        suggestion_id=suggestion.id,
        title=suggestion.proposed_title or "",
        date=suggestion.effective_date,
        correspondent_id=suggestion.effective_correspondent_id,
        doctype_id=suggestion.effective_doctype_id,
        storage_path_id=suggestion.effective_storage_path_id,
        tag_ids=tag_ids,
        action="accept",
    )

    await commit_suggestion(suggestion, decision, _paperless)
    await _telegram.answer_callback_query(cb_id, "Committed!")
    if chat_id and message_id:
        await _telegram.edit_message_text(
            chat_id,
            message_id,
            f"\u2705 <b>Committed</b> — Document #{suggestion.document_id}\n"
            f"<b>{suggestion.proposed_title}</b>",
        )
    log.info("suggestion accepted via telegram", suggestion_id=suggestion.id)


async def _reject_via_telegram(
    suggestion: SuggestionRow, cb_id: str, chat_id: int | str, message_id: int
) -> None:
    """Reject a suggestion via Telegram."""
    with get_conn() as conn:
        conn.execute(
            "UPDATE suggestions SET status = 'rejected' WHERE id = ?",
            (suggestion.id,),
        )
        conn.execute(
            """
            INSERT INTO audit_log (action, document_id, actor, details)
            VALUES ('reject', ?, 'telegram', NULL)
            """,
            (suggestion.document_id,),
        )

    await _telegram.answer_callback_query(cb_id, "Rejected")
    if chat_id and message_id:
        await _telegram.edit_message_text(
            chat_id,
            message_id,
            f"\u274c <b>Rejected</b> — Document #{suggestion.document_id}\n"
            f"<b>{suggestion.proposed_title}</b>",
        )
    log.info("suggestion rejected via telegram", suggestion_id=suggestion.id)


# ----------------------------------------------------------------------
# Chat handler: process incoming text messages via RAG
# ----------------------------------------------------------------------
async def _handle_message(update: dict) -> None:
    """Process a regular text message as a RAG chat question."""
    message = update.get("message", {})
    text = message.get("text", "").strip()
    chat_id = message.get("chat", {}).get("id")

    if not text or not chat_id:
        return

    # Skip Telegram commands (future extension)
    if text.startswith("/"):
        return

    if not _ollama or not _paperless or not _telegram:
        return

    from app.chat import ask, get_or_create_session

    session_key = f"tg:{chat_id}"
    _, session = get_or_create_session(session_key)

    try:
        result = await ask(text, session, _paperless, _ollama)
    except Exception as exc:
        log.error("telegram chat failed", error=str(exc), chat_id=chat_id)
        await _telegram.send_message(f"Fehler bei der Verarbeitung: {exc}", parse_mode="HTML")
        return

    # Build response with optional source references
    answer = result.answer
    if result.sources:
        source_lines = ", ".join(f"#{s['id']} {s['title']}" for s in result.sources[:5])
        answer += f"\n\n<i>Quellen: {source_lines}</i>"

    await _telegram.send_message(answer, parse_mode="HTML")
    log.info("telegram chat response sent", chat_id=chat_id)


# ----------------------------------------------------------------------
# Polling loop
# ----------------------------------------------------------------------
async def _poll_loop() -> None:
    """Background task: poll Telegram for callback queries and messages."""
    log.info("telegram poll loop started")
    while True:
        updates = await _telegram.get_updates(timeout=settings.telegram_poll_interval)
        for update in updates:
            try:
                if "callback_query" in update:
                    await _handle_callback(update)
                elif "message" in update and "text" in update.get("message", {}):
                    await _handle_message(update)
            except Exception as exc:
                log.warning("telegram update error", error=str(exc))
        await asyncio.sleep(0.5)


# ----------------------------------------------------------------------
# Lifecycle (called from main.py)
# ----------------------------------------------------------------------
def start_telegram(
    telegram: TelegramClient,
    paperless: PaperlessClient,
    ollama: OllamaClient | None = None,
) -> None:
    """Start the Telegram update-polling background task."""
    global _telegram, _paperless, _ollama, _poll_task

    _telegram = telegram
    _paperless = paperless
    _ollama = ollama

    if not telegram.enabled:
        log.info("telegram disabled — skipping")
        return

    _poll_task = asyncio.get_event_loop().create_task(_poll_loop())
    log.info("telegram handler started")


def stop_telegram() -> None:
    """Cancel the polling task."""
    global _poll_task
    if _poll_task:
        _poll_task.cancel()
        _poll_task = None
        log.info("telegram handler stopped")
