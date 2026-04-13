"""Tests for OCR correction heuristic."""

from unittest.mock import AsyncMock, patch

import pytest

from app.models import PaperlessDocument
from app.pipeline.ocr_correction import _text_looks_broken, maybe_correct_ocr


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
        # Simulate OCR that splits words: "Rechnung" -> "R e c h n u n g"
        text = "R e c h n u n g N r 1 2 3 4 5 v o m S t a d t w e r k e " * 3
        assert _text_looks_broken(text) is True

    def test_non_standard_characters(self):
        """High ratio of unusual characters."""
        # Mix in lots of weird unicode artifacts
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
        # 200 chars with 3 question marks = 1.5% < 2%
        text = "A" * 197 + "???"
        assert _text_looks_broken(text) is False

    def test_over_question_mark_threshold(self):
        """Just over 2% threshold should trigger."""
        # 100 chars with 3 question marks = 3% > 2%
        text = "A" * 97 + "???"
        assert _text_looks_broken(text) is True


class TestOcrUsesOcrModel:
    """Verify that OCR correction passes the dedicated OCR model to chat_json."""

    @pytest.mark.asyncio
    async def test_ocr_passes_ocr_model(self):
        """maybe_correct_ocr should call chat_json with model=ollama.ocr_model."""
        # Enough question marks to trigger the broken-text heuristic
        doc = PaperlessDocument(id=1, title="Test", content="?" * 100, tags=[99])
        mock_ollama = AsyncMock()
        mock_ollama.ocr_model = "gemma3:1b"
        mock_ollama.chat_json = AsyncMock(
            return_value={"corrected_text": "fixed text", "num_corrections": 5}
        )

        with patch("app.pipeline.ocr_correction.settings") as mock_settings:
            mock_settings.enable_ocr_correction = True
            mock_settings.max_doc_chars = 8000
            mock_settings.prompts_dir.__truediv__ = lambda self, x: type(
                "P", (), {"read_text": lambda self, **kw: "system prompt"}
            )()

            await maybe_correct_ocr(doc, mock_ollama)

        mock_ollama.chat_json.assert_called_once()
        _, kwargs = mock_ollama.chat_json.call_args
        assert kwargs["model"] == "gemma3:1b"
