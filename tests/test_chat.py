"""Tests for the RAG chat feature — OllamaClient.chat(), find_similar_by_query_text(),
session management, ask() pipeline, and Telegram message handling."""

from __future__ import annotations

import time
from unittest.mock import AsyncMock, patch

import httpx
import pytest

from app.chat import (
    MAX_HISTORY,
    SESSION_TTL,
    ChatSession,
    _sessions,
    ask,
    get_or_create_session,
    load_chat_system_prompt,
)
from app.clients.ollama import OllamaClient
from app.models import PaperlessDocument


# =====================================================================
# OllamaClient.chat()
# =====================================================================
class TestOllamaChatMethod:
    @pytest.fixture()
    def mock_response(self):
        resp = AsyncMock(spec=httpx.Response)
        resp.status_code = 200
        resp.json.return_value = {
            "message": {"role": "assistant", "content": "Das ist eine Antwort."}
        }
        resp.raise_for_status = lambda: None
        return resp

    @pytest.mark.asyncio()
    async def test_chat_returns_plain_text(self, mock_response):
        client = OllamaClient.__new__(OllamaClient)
        client.model = "test-model"
        client._client = AsyncMock()
        client._client.post = AsyncMock(return_value=mock_response)

        messages = [
            {"role": "system", "content": "Du bist ein Assistent."},
            {"role": "user", "content": "Hallo"},
        ]
        result = await client.chat(messages)

        assert result == "Das ist eine Antwort."
        assert isinstance(result, str)

    @pytest.mark.asyncio()
    async def test_chat_sends_full_messages_list(self, mock_response):
        client = OllamaClient.__new__(OllamaClient)
        client.model = "test-model"
        client._client = AsyncMock()
        client._client.post = AsyncMock(return_value=mock_response)

        messages = [
            {"role": "system", "content": "system"},
            {"role": "user", "content": "q1"},
            {"role": "assistant", "content": "a1"},
            {"role": "user", "content": "q2"},
        ]
        await client.chat(messages)

        call_args = client._client.post.call_args
        payload = call_args.kwargs.get("json") or call_args[1].get("json")
        assert payload["messages"] == messages
        # No format="json" in payload
        assert "format" not in payload

    @pytest.mark.asyncio()
    async def test_chat_raises_on_empty_content(self):
        client = OllamaClient.__new__(OllamaClient)
        client.model = "test-model"
        client._client = AsyncMock()

        resp = AsyncMock(spec=httpx.Response)
        resp.status_code = 200
        resp.json.return_value = {"message": {"role": "assistant", "content": ""}}
        resp.raise_for_status = lambda: None
        client._client.post = AsyncMock(return_value=resp)

        with pytest.raises(ValueError, match="empty content"):
            await client.chat([{"role": "user", "content": "test"}])


# =====================================================================
# Session management
# =====================================================================
class TestSessionManagement:
    @pytest.fixture(autouse=True)
    def _clear_sessions(self):
        _sessions.clear()
        yield
        _sessions.clear()

    def test_creates_new_session(self):
        sid, session = get_or_create_session(None)
        assert sid
        assert len(sid) == 16
        assert isinstance(session, ChatSession)
        assert session.messages == []

    def test_reuses_existing_session(self):
        sid1, session1 = get_or_create_session(None)
        session1.messages.append({"role": "user", "content": "test"})

        sid2, session2 = get_or_create_session(sid1)
        assert sid2 == sid1
        assert session2 is session1
        assert len(session2.messages) == 1

    def test_creates_new_for_unknown_id(self):
        sid, session = get_or_create_session("nonexistent")
        assert sid != "nonexistent"
        assert session.messages == []

    def test_expires_old_sessions(self):
        sid, session = get_or_create_session(None)
        session.last_active = time.time() - SESSION_TTL - 1

        new_sid, new_session = get_or_create_session(sid)
        assert new_sid != sid
        assert new_session is not session


# =====================================================================
# ask() pipeline
# =====================================================================
class TestAskPipeline:
    @pytest.mark.asyncio()
    async def test_ask_returns_answer_and_sources(self):
        session = ChatSession()
        mock_paperless = AsyncMock()
        mock_paperless.list_correspondents = AsyncMock(return_value=[])
        mock_paperless.list_document_types = AsyncMock(return_value=[])
        mock_paperless.list_storage_paths = AsyncMock(return_value=[])
        mock_paperless.list_tags = AsyncMock(return_value=[])

        mock_ollama = AsyncMock()
        mock_ollama.chat = AsyncMock(return_value="Die letzte Rechnung war vom 15.03.2024.")

        mock_doc = PaperlessDocument(id=42, title="Stromrechnung Q1", content="Rechnung...")

        with patch("app.chat.find_similar_by_query_text") as mock_find:
            from app.pipeline.context_builder import SimilarDocument

            mock_find.return_value = [SimilarDocument(document=mock_doc, distance=0.15)]

            result = await ask(
                "Wann war meine letzte Rechnung?", session, mock_paperless, mock_ollama
            )

        assert "15.03.2024" in result.answer
        assert len(result.sources) == 1
        assert result.sources[0]["id"] == 42
        assert result.sources[0]["title"] == "Stromrechnung Q1"

    @pytest.mark.asyncio()
    async def test_ask_appends_to_session_history(self):
        session = ChatSession()
        mock_paperless = AsyncMock()
        mock_ollama = AsyncMock()
        mock_ollama.chat = AsyncMock(return_value="Antwort")

        with patch("app.chat.find_similar_by_query_text", return_value=[]):
            await ask("Frage", session, mock_paperless, mock_ollama)

        assert len(session.messages) == 2
        assert session.messages[0] == {"role": "user", "content": "Frage"}
        assert session.messages[1] == {"role": "assistant", "content": "Antwort"}

    @pytest.mark.asyncio()
    async def test_ask_trims_history(self):
        session = ChatSession()
        # Fill history to near max
        for i in range(MAX_HISTORY):
            session.messages.append({"role": "user", "content": f"q{i}"})

        mock_paperless = AsyncMock()
        mock_ollama = AsyncMock()
        mock_ollama.chat = AsyncMock(return_value="Antwort")

        with patch("app.chat.find_similar_by_query_text", return_value=[]):
            await ask("Neue Frage", session, mock_paperless, mock_ollama)

        assert len(session.messages) <= MAX_HISTORY

    @pytest.mark.asyncio()
    async def test_ask_handles_no_similar_docs(self):
        session = ChatSession()
        mock_paperless = AsyncMock()
        mock_ollama = AsyncMock()
        mock_ollama.chat = AsyncMock(return_value="Keine Dokumente gefunden.")

        with patch("app.chat.find_similar_by_query_text", return_value=[]):
            result = await ask("Gibt es Vertraege?", session, mock_paperless, mock_ollama)

        assert result.answer == "Keine Dokumente gefunden."
        assert result.sources == []
        # Entity lists should NOT be fetched when no similar docs found
        mock_paperless.list_correspondents.assert_not_called()

    @pytest.mark.asyncio()
    async def test_ask_handles_llm_error(self):
        session = ChatSession()
        mock_paperless = AsyncMock()
        mock_ollama = AsyncMock()
        mock_ollama.chat = AsyncMock(side_effect=Exception("connection refused"))

        with patch("app.chat.find_similar_by_query_text", return_value=[]):
            result = await ask("Frage", session, mock_paperless, mock_ollama)

        assert "Fehler" in result.answer


# =====================================================================
# System prompt loading
# =====================================================================
class TestChatSystemPrompt:
    def test_loads_default_prompt(self):
        prompt = load_chat_system_prompt()
        assert "Paperless" in prompt
        assert "Dokumente" in prompt


# =====================================================================
# Telegram message handling
# =====================================================================
class TestTelegramChatHandler:
    @pytest.fixture(autouse=True)
    def _clear_sessions(self):
        _sessions.clear()
        yield
        _sessions.clear()

    @pytest.mark.asyncio()
    async def test_handle_message_sends_response(self):
        import app.telegram_handler as th
        from app.telegram_handler import _handle_message

        mock_telegram = AsyncMock()
        mock_telegram.send_message = AsyncMock()
        mock_paperless = AsyncMock()
        mock_ollama = AsyncMock()

        old_tg, old_pl, old_ol = th._telegram, th._paperless, th._ollama
        th._telegram = mock_telegram
        th._paperless = mock_paperless
        th._ollama = mock_ollama

        try:
            with patch("app.chat.ask") as mock_ask:
                from app.chat import ChatResult

                mock_ask.return_value = ChatResult(
                    answer="Das ist die Antwort.",
                    sources=[{"id": 1, "title": "Doc", "distance": 0.1}],
                )

                update = {
                    "message": {
                        "text": "Wann war meine letzte Rechnung?",
                        "chat": {"id": 12345},
                    }
                }
                await _handle_message(update)

            mock_telegram.send_message.assert_called_once()
            sent_text = mock_telegram.send_message.call_args[0][0]
            assert "Antwort" in sent_text
            assert "Quellen" in sent_text
        finally:
            th._telegram = old_tg
            th._paperless = old_pl
            th._ollama = old_ol

    @pytest.mark.asyncio()
    async def test_handle_message_skips_commands(self):
        import app.telegram_handler as th
        from app.telegram_handler import _handle_message

        mock_telegram = AsyncMock()
        old_tg = th._telegram
        th._telegram = mock_telegram

        try:
            update = {
                "message": {
                    "text": "/start",
                    "chat": {"id": 12345},
                }
            }
            await _handle_message(update)
            mock_telegram.send_message.assert_not_called()
        finally:
            th._telegram = old_tg
