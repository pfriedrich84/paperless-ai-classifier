"""Tests for Pydantic model validation."""

import pytest
from pydantic import ValidationError

from app.models import ClassificationResult, PaperlessDocument, ReviewDecision, SuggestionRow


class TestClassificationResult:
    def test_valid_full_response(self):
        data = {
            "title": "Stromrechnung März 2024",
            "date": "2024-03-15",
            "correspondent": "Stadtwerke München",
            "document_type": "Rechnung",
            "storage_path": "Finanzen/Strom",
            "tags": [
                {"name": "Finanzen", "confidence": 90},
                {"name": "Strom", "confidence": 80},
            ],
            "confidence": 85,
            "reasoning": "Erkannt als monatliche Stromrechnung",
        }
        result = ClassificationResult.model_validate(data)
        assert result.title == "Stromrechnung März 2024"
        assert result.date == "2024-03-15"
        assert result.confidence == 85
        assert len(result.tags) == 2
        assert result.tags[0].name == "Finanzen"
        assert result.tags[0].confidence == 90

    def test_minimal_response(self):
        data = {"title": "Unbekanntes Dokument"}
        result = ClassificationResult.model_validate(data)
        assert result.title == "Unbekanntes Dokument"
        assert result.date is None
        assert result.correspondent is None
        assert result.confidence == 50  # default
        assert result.tags == []

    def test_null_fields(self):
        data = {
            "title": "Test",
            "date": None,
            "correspondent": None,
            "document_type": None,
            "storage_path": None,
            "tags": [],
            "confidence": 30,
            "reasoning": "",
        }
        result = ClassificationResult.model_validate(data)
        assert result.date is None
        assert result.correspondent is None

    def test_tags_default_confidence(self):
        data = {
            "title": "Test",
            "tags": [{"name": "SomeTag"}],
        }
        result = ClassificationResult.model_validate(data)
        assert result.tags[0].confidence == 50

    def test_missing_title_raises(self):
        with pytest.raises(ValidationError):
            ClassificationResult.model_validate({"confidence": 80})

    def test_extra_fields_ignored(self):
        data = {
            "title": "Test",
            "unknown_field": "should be fine",
            "another": 123,
        }
        # Should not raise — extra fields are fine
        result = ClassificationResult.model_validate(data)
        assert result.title == "Test"


class TestPaperlessDocument:
    def test_minimal_document(self):
        doc = PaperlessDocument(id=1, title="test.pdf")
        assert doc.content == ""
        assert doc.tags == []
        assert doc.correspondent is None

    def test_full_document(self):
        doc = PaperlessDocument(
            id=42,
            title="Invoice",
            content="Some text",
            correspondent=5,
            document_type=3,
            storage_path=1,
            tags=[10, 20, 30],
        )
        assert doc.tags == [10, 20, 30]
        assert doc.correspondent == 5

    def test_extra_fields_ignored(self):
        """Paperless API returns many more fields — they should be ignored."""
        data = {
            "id": 1,
            "title": "test",
            "archive_serial_number": None,
            "original_file_name": "scan.pdf",
            "notes": [],
        }
        doc = PaperlessDocument.model_validate(data)
        assert doc.id == 1


class TestReviewDecision:
    def test_accept_decision(self):
        d = ReviewDecision(
            suggestion_id=1,
            title="Test",
            action="accept",
            tag_ids=[1, 2, 3],
        )
        assert d.action == "accept"
        assert d.tag_ids == [1, 2, 3]
        assert d.date is None

    def test_invalid_action_raises(self):
        with pytest.raises(ValidationError):
            ReviewDecision(
                suggestion_id=1,
                title="Test",
                action="invalid",
            )


class TestSuggestionRowEffective:
    """Tests for effective_* fallback properties on SuggestionRow."""

    def _make_row(self, **overrides) -> SuggestionRow:
        defaults = {
            "id": 1,
            "document_id": 42,
            "created_at": "2024-01-01T00:00:00",
            "status": "pending",
        }
        defaults.update(overrides)
        return SuggestionRow(**defaults)

    def test_effective_uses_proposed_when_set(self):
        s = self._make_row(
            original_date="2024-01-01",
            proposed_date="2024-06-15",
            original_correspondent=1,
            proposed_correspondent_id=2,
            original_doctype=10,
            proposed_doctype_id=20,
            original_storage_path=30,
            proposed_storage_path_id=40,
        )
        assert s.effective_date == "2024-06-15"
        assert s.effective_correspondent_id == 2
        assert s.effective_doctype_id == 20
        assert s.effective_storage_path_id == 40

    def test_effective_falls_back_to_original(self):
        s = self._make_row(
            original_date="2024-01-01",
            proposed_date=None,
            original_correspondent=1,
            proposed_correspondent_id=None,
            original_doctype=10,
            proposed_doctype_id=None,
            original_storage_path=30,
            proposed_storage_path_id=None,
        )
        assert s.effective_date == "2024-01-01"
        assert s.effective_correspondent_id == 1
        assert s.effective_doctype_id == 10
        assert s.effective_storage_path_id == 30

    def test_effective_returns_none_when_both_null(self):
        s = self._make_row()
        assert s.effective_date is None
        assert s.effective_correspondent_id is None
        assert s.effective_doctype_id is None
        assert s.effective_storage_path_id is None
