"""Telegram Bot API client for approval notifications."""

from __future__ import annotations

import json
from typing import Any

import httpx
import structlog

from app.config import settings

log = structlog.get_logger(__name__)


class TelegramClient:
    """Thin httpx wrapper around the Telegram Bot API."""

    def __init__(
        self,
        token: str | None = None,
        chat_id: str | None = None,
    ) -> None:
        self.token = token or settings.telegram_bot_token
        self.chat_id = chat_id or settings.telegram_chat_id
        self._base = f"https://api.telegram.org/bot{self.token}"
        self._client = httpx.AsyncClient(timeout=30.0)
        self._offset: int = 0  # for long-polling getUpdates

    async def aclose(self) -> None:
        await self._client.aclose()

    @property
    def enabled(self) -> bool:
        return bool(settings.enable_telegram and self.token and self.chat_id)

    # ------------------------------------------------------------------
    # Sending
    # ------------------------------------------------------------------
    async def send_message(
        self,
        text: str,
        reply_markup: dict[str, Any] | None = None,
        parse_mode: str = "HTML",
    ) -> dict[str, Any] | None:
        """Send a message to the configured chat."""
        if not self.enabled:
            return None
        payload: dict[str, Any] = {
            "chat_id": self.chat_id,
            "text": text,
            "parse_mode": parse_mode,
        }
        if reply_markup:
            payload["reply_markup"] = json.dumps(reply_markup)
        try:
            r = await self._client.post(f"{self._base}/sendMessage", data=payload)
            r.raise_for_status()
            return r.json()
        except Exception as exc:
            log.warning("telegram send failed", error=str(exc))
            return None

    async def answer_callback_query(
        self,
        callback_query_id: str,
        text: str = "",
    ) -> None:
        """Acknowledge an inline-keyboard button press."""
        try:
            await self._client.post(
                f"{self._base}/answerCallbackQuery",
                data={"callback_query_id": callback_query_id, "text": text},
            )
        except Exception as exc:
            log.warning("telegram answer_callback failed", error=str(exc))

    async def edit_message_text(
        self,
        chat_id: str | int,
        message_id: int,
        text: str,
        parse_mode: str = "HTML",
    ) -> None:
        """Edit an existing message (e.g. remove inline keyboard after action)."""
        try:
            await self._client.post(
                f"{self._base}/editMessageText",
                data={
                    "chat_id": chat_id,
                    "message_id": message_id,
                    "text": text,
                    "parse_mode": parse_mode,
                },
            )
        except Exception as exc:
            log.warning("telegram edit_message failed", error=str(exc))

    # ------------------------------------------------------------------
    # Receiving (long-polling)
    # ------------------------------------------------------------------
    async def get_updates(self, timeout: int = 1) -> list[dict[str, Any]]:
        """Fetch new updates via long-polling."""
        if not self.enabled:
            return []
        try:
            r = await self._client.get(
                f"{self._base}/getUpdates",
                params={
                    "offset": self._offset,
                    "timeout": timeout,
                    "allowed_updates": '["callback_query"]',
                },
                timeout=timeout + 10,
            )
            r.raise_for_status()
            data = r.json()
            updates = data.get("result", [])
            if updates:
                self._offset = updates[-1]["update_id"] + 1
            return updates
        except Exception as exc:
            log.warning("telegram getUpdates failed", error=str(exc))
            return []
