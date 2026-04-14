"""Tests for OCR correction: heuristic, mode dispatch, vision, fallback, and cache."""

from unittest.mock import AsyncMock, patch

import pytest

from app.models import PaperlessDocument
from app.pipeline.ocr_correction import (
    _split_text_by_pages,
    _text_looks_broken,
    cache_ocr_correction,
    effective_ocr_mode,
    get_cached_ocr,
    maybe_correct_ocr,
)


# ---------------------------------------------------------------------------
# Heuristic tests
# ---------------------------------------------------------------------------
class TestTextLooksBroken:
    def test_clean_german_text(self):
        text = (
            "Sehr geehrte Damen und Herren,\n\n"
            "hiermit bestätigen wir den Eingang Ihrer Zahlung in Höhe von 87,50 EUR.\n"
            "Die Rechnung Nr. 12345 vom 15.03.2024 ist damit beglichen.\n\n"
            "Mit freundlichen Grüßen\nStadtwerke München GmbH"
        )
        assert _text_looks_broken(text) is False

    def test_too_short_text(self):
        """Texts under 50 chars should never trigger correction."""
        assert _text_looks_broken("Short") is False
        assert _text_looks_broken("") is False
        assert _text_looks_broken("x" * 49) is False

    def test_many_question_marks(self):
        """High ? ratio indicates unrecognized glyphs."""
        text = "Rech?ung Nr. 123?5 vom ?5.03.20?4 Stadtw?rke M?nch?n GmbH " * 5
        assert _text_looks_broken(text) is True

    def test_many_single_char_words(self):
        """Many single-char words indicate broken tokenization."""
        text = "R e c h n u n g N r 1 2 3 4 5 v o m S t a d t w e r k e " * 3
        assert _text_looks_broken(text) is True

    def test_non_standard_characters(self):
        """High ratio of unusual characters."""
        text = "Rechnüng" + "\x00\x01\x02\x03\x04\x05" * 20 + "x" * 50
        assert _text_looks_broken(text) is True

    def test_normal_single_char_words_ok(self):
        """Normal single-char words (articles, abbreviations) shouldn't trigger."""
        text = (
            "Ich bin ein Bürger und zahle Steuern. "
            "Das ist eine Rechnung für Strom und Gas. "
            "Am 15. März habe ich den Betrag überwiesen."
        ) * 3
        assert _text_looks_broken(text) is False

    def test_borderline_question_marks(self):
        """Just under 2% threshold should pass."""
        text = "A" * 197 + "???"
        assert _text_looks_broken(text) is False

    def test_over_question_mark_threshold(self):
        """Just over 2% threshold should trigger."""
        text = "A" * 97 + "???"
        assert _text_looks_broken(text) is True


# ---------------------------------------------------------------------------
# effective_ocr_mode()
# ---------------------------------------------------------------------------
class TestEffectiveOcrMode:
    def test_ocr_mode_off(self):
        with patch("app.pipeline.ocr_correction.settings") as s:
            s.ocr_mode = "off"
            s.enable_ocr_correction = False
            assert effective_ocr_mode() == "off"

    def test_ocr_mode_text(self):
        with patch("app.pipeline.ocr_correction.settings") as s:
            s.ocr_mode = "text"
            s.enable_ocr_correction = False
            assert effective_ocr_mode() == "text"

    def test_ocr_mode_vision_light(self):
        with patch("app.pipeline.ocr_correction.settings") as s:
            s.ocr_mode = "vision_light"
            s.enable_ocr_correction = False
            assert effective_ocr_mode() == "vision_light"

    def test_ocr_mode_vision_full(self):
        with patch("app.pipeline.ocr_correction.settings") as s:
            s.ocr_mode = "vision_full"
            s.enable_ocr_correction = False
            assert effective_ocr_mode() == "vision_full"

    def test_backwards_compat_enable_ocr_correction(self):
        """enable_ocr_correction=True with ocr_mode=off -> text mode."""
        with patch("app.pipeline.ocr_correction.settings") as s:
            s.ocr_mode = "off"
            s.enable_ocr_correction = True
            assert effective_ocr_mode() == "text"

    def test_invalid_mode_falls_back_to_off(self):
        with patch("app.pipeline.ocr_correction.settings") as s:
            s.ocr_mode = "invalid_mode"
            s.enable_ocr_correction = False
            assert effective_ocr_mode() == "off"


# ---------------------------------------------------------------------------
# Mode dispatch
# ---------------------------------------------------------------------------
class TestMaybeCorrectOcr:
    @pytest.mark.asyncio
    async def test_off_mode_returns_original(self):
        doc = PaperlessDocument(id=1, title="Test", content="Some text", tags=[])
        mock_ollama = AsyncMock()

        with patch("app.pipeline.ocr_correction.effective_ocr_mode", return_value="off"):
            text, num = await maybe_correct_ocr(doc, mock_ollama)

        assert text == "Some text"
        assert num == 0

    @pytest.mark.asyncio
    async def test_text_mode_uses_ocr_model(self):
        """Text mode should call chat_json with model=ollama.ocr_model."""
        doc = PaperlessDocument(id=1, title="Test", content="?" * 100, tags=[99])
        mock_ollama = AsyncMock()
        mock_ollama.ocr_model = "gemma3:1b"
        mock_ollama.chat_json = AsyncMock(
            return_value={"corrected_text": "fixed text", "num_corrections": 5}
        )

        with (
            patch("app.pipeline.ocr_correction.effective_ocr_mode", return_value="text"),
            patch("app.pipeline.ocr_correction.settings") as mock_settings,
        ):
            mock_settings.max_doc_chars = 8000
            mock_settings.prompts_dir.__truediv__ = lambda self, x: type(
                "P", (), {"read_text": lambda self, **kw: "system prompt"}
            )()

            text, num = await maybe_correct_ocr(doc, mock_ollama)

        assert text == "fixed text"
        assert num == 5
        mock_ollama.chat_json.assert_called_once()
        _, kwargs = mock_ollama.chat_json.call_args
        assert kwargs["model"] == "gemma3:1b"

    @pytest.mark.asyncio
    async def test_text_mode_skips_clean_text(self):
        """Text mode should skip correction when text looks fine."""
        clean = "Sehr geehrte Damen und Herren, wir bestaetigen den Eingang. " * 3
        doc = PaperlessDocument(id=1, title="Test", content=clean, tags=[])
        mock_ollama = AsyncMock()

        with patch("app.pipeline.ocr_correction.effective_ocr_mode", return_value="text"):
            text, num = await maybe_correct_ocr(doc, mock_ollama)

        assert text == clean
        assert num == 0

    @pytest.mark.asyncio
    async def test_vision_light_falls_back_without_paperless(self):
        """vision_light without paperless client should fall back to text mode."""
        doc = PaperlessDocument(id=1, title="Test", content="?" * 100, tags=[])
        mock_ollama = AsyncMock()
        mock_ollama.ocr_model = "gemma3:1b"
        mock_ollama.chat_json = AsyncMock(
            return_value={"corrected_text": "text fixed", "num_corrections": 3}
        )

        with (
            patch("app.pipeline.ocr_correction.effective_ocr_mode", return_value="vision_light"),
            patch("app.pipeline.ocr_correction.settings") as mock_settings,
        ):
            mock_settings.max_doc_chars = 8000
            mock_settings.prompts_dir.__truediv__ = lambda self, x: type(
                "P", (), {"read_text": lambda self, **kw: "system prompt"}
            )()

            text, num = await maybe_correct_ocr(doc, mock_ollama, paperless=None)

        # Should have fallen back to text mode
        assert text == "text fixed"
        assert num == 3

    @pytest.mark.asyncio
    async def test_vision_full_always_runs(self):
        """vision_full should run even when text looks fine (no heuristic gate)."""
        clean = "Sehr geehrte Damen und Herren, wir bestaetigen den Eingang. " * 3
        doc = PaperlessDocument(id=1, title="Test", content=clean, tags=[])
        mock_ollama = AsyncMock()
        mock_ollama.model = "gemma4:e2b"
        mock_ollama.chat_vision_json = AsyncMock(
            return_value={"corrected_text": "improved text", "num_corrections": 1}
        )
        mock_paperless = AsyncMock()
        mock_paperless.download_document = AsyncMock(return_value=(b"%PDF-fake", "application/pdf"))

        with (
            patch("app.pipeline.ocr_correction.effective_ocr_mode", return_value="vision_full"),
            patch("app.pipeline.ocr_correction.settings") as mock_settings,
            patch(
                "app.pipeline.ocr_correction._render_pages",
                return_value=["base64image1"],
            ),
        ):
            mock_settings.ocr_vision_model = ""
            mock_settings.ocr_vision_max_pages = 1
            mock_settings.ocr_vision_dpi = 150
            mock_settings.max_doc_chars = 8000
            mock_settings.prompts_dir.__truediv__ = lambda self, x: type(
                "P", (), {"read_text": lambda self, **kw: "system prompt"}
            )()

            _text, num = await maybe_correct_ocr(doc, mock_ollama, mock_paperless)

        # Should have run despite clean text
        assert num == 1
        mock_paperless.download_document.assert_called_once_with(doc.id)

    @pytest.mark.asyncio
    async def test_text_mode_passes_ocr_num_ctx(self):
        """Text mode should pass ollama_ocr_num_ctx to chat_json."""
        doc = PaperlessDocument(id=1, title="Test", content="?" * 100, tags=[99])
        mock_ollama = AsyncMock()
        mock_ollama.ocr_model = "gemma3:1b"
        mock_ollama.chat_json = AsyncMock(
            return_value={"corrected_text": "fixed", "num_corrections": 1}
        )

        with (
            patch("app.pipeline.ocr_correction.effective_ocr_mode", return_value="text"),
            patch("app.pipeline.ocr_correction.settings") as mock_settings,
        ):
            mock_settings.max_doc_chars = 8000
            mock_settings.ollama_ocr_num_ctx = 131072
            mock_settings.prompts_dir.__truediv__ = lambda self, x: type(
                "P", (), {"read_text": lambda self, **kw: "system prompt"}
            )()

            await maybe_correct_ocr(doc, mock_ollama)

        call_kwargs = mock_ollama.chat_json.call_args.kwargs
        assert call_kwargs["num_ctx"] == 131072

    @pytest.mark.asyncio
    async def test_vision_full_passes_ocr_num_ctx(self):
        """vision_full should pass ollama_ocr_num_ctx to chat_vision_json."""
        clean = "Sehr geehrte Damen und Herren, wir bestaetigen den Eingang. " * 3
        doc = PaperlessDocument(id=1, title="Test", content=clean, tags=[])
        mock_ollama = AsyncMock()
        mock_ollama.model = "gemma4:e2b"
        mock_ollama.chat_vision_json = AsyncMock(
            return_value={"corrected_text": "improved text", "num_corrections": 1}
        )
        mock_paperless = AsyncMock()
        mock_paperless.download_document = AsyncMock(return_value=(b"%PDF-fake", "application/pdf"))

        with (
            patch("app.pipeline.ocr_correction.effective_ocr_mode", return_value="vision_full"),
            patch("app.pipeline.ocr_correction.settings") as mock_settings,
            patch(
                "app.pipeline.ocr_correction._render_pages",
                return_value=["base64image1"],
            ),
        ):
            mock_settings.ocr_vision_model = ""
            mock_settings.ocr_vision_max_pages = 1
            mock_settings.ocr_vision_dpi = 150
            mock_settings.max_doc_chars = 8000
            mock_settings.ollama_ocr_num_ctx = 131072
            mock_settings.prompts_dir.__truediv__ = lambda self, x: type(
                "P", (), {"read_text": lambda self, **kw: "system prompt"}
            )()

            await maybe_correct_ocr(doc, mock_ollama, mock_paperless)

        call_kwargs = mock_ollama.chat_vision_json.call_args.kwargs
        assert call_kwargs["num_ctx"] == 131072


# ---------------------------------------------------------------------------
# Text splitting
# ---------------------------------------------------------------------------
class TestSplitTextByPages:
    def test_form_feed_split(self):
        text = "Page 1 content\fPage 2 content\fPage 3 content"
        result = _split_text_by_pages(text, 3)
        assert result == ["Page 1 content", "Page 2 content", "Page 3 content"]

    def test_form_feed_fewer_pages(self):
        text = "Page 1\fPage 2\fPage 3"
        result = _split_text_by_pages(text, 2)
        assert result == ["Page 1", "Page 2"]

    def test_form_feed_more_pages(self):
        text = "Page 1\fPage 2"
        result = _split_text_by_pages(text, 4)
        assert len(result) == 4
        assert result[0] == "Page 1"
        assert result[1] == "Page 2"
        assert result[2] == ""
        assert result[3] == ""

    def test_no_form_feed_even_split(self):
        text = "AABBCC"
        result = _split_text_by_pages(text, 3)
        assert len(result) == 3
        assert "".join(result) == text

    def test_single_page(self):
        text = "All on one page"
        result = _split_text_by_pages(text, 1)
        assert result == ["All on one page"]


# ---------------------------------------------------------------------------
# OCR cache (using real SQLite via conftest)
# ---------------------------------------------------------------------------
class TestOcrCache:
    def test_cache_round_trip(self, tmp_path):
        """Cache stores and retrieves corrected text."""
        db_path = tmp_path / "test.db"
        with patch("app.pipeline.ocr_correction.settings") as s:
            s.db_path = db_path

            with patch("app.pipeline.ocr_correction.get_conn") as mock_conn:
                # Use a real in-memory sqlite connection for this test
                import sqlite3

                conn = sqlite3.connect(":memory:")
                conn.row_factory = sqlite3.Row
                conn.execute(
                    """CREATE TABLE doc_ocr_cache (
                        document_id INTEGER PRIMARY KEY,
                        corrected_content TEXT NOT NULL,
                        ocr_mode TEXT NOT NULL,
                        num_corrections INTEGER NOT NULL DEFAULT 0,
                        corrected_at TEXT NOT NULL DEFAULT (datetime('now'))
                    )"""
                )

                from contextlib import contextmanager

                @contextmanager
                def fake_conn():
                    yield conn

                mock_conn.side_effect = fake_conn

                # No cache yet
                assert get_cached_ocr(42) is None

                # Store
                cache_ocr_correction(42, "corrected text", "vision_full", 5)

                # Retrieve
                cached = get_cached_ocr(42)
                assert cached == "corrected text"

                # Overwrite
                cache_ocr_correction(42, "updated", "text", 2)
                assert get_cached_ocr(42) == "updated"

                conn.close()
