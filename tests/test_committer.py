"""Tests for the committer pipeline."""

import pytest

from app.models import PaperlessDocument, ReviewDecision, SuggestionRow
from app.pipeline.committer import commit_suggestion


def _make_suggestion(**overrides) -> SuggestionRow:
    defaults = {
        "id": 1,
        "document_id": 42,
        "created_at": "2024-03-15T10:00:00",
        "status": "pending",
        "confidence": 85,
        "proposed_title": "Stromrechnung",
        "proposed_correspondent_id": 5,
        "proposed_doctype_id": 3,
    }
    defaults.update(overrides)
    return SuggestionRow(**defaults)


def _make_decision(**overrides) -> ReviewDecision:
    defaults = {
        "suggestion_id": 1,
        "title": "Stromrechnung März 2024",
        "date": "2024-03-15",
        "correspondent_id": 5,
        "doctype_id": 3,
        "storage_path_id": None,
        "tag_ids": [20, 21],
        "action": "accept",
    }
    defaults.update(overrides)
    return ReviewDecision(**defaults)


@pytest.mark.asyncio
async def test_commit_builds_correct_fields(mock_paperless, patch_db, monkeypatch):
    """Verify the PATCH payload is assembled correctly."""
    monkeypatch.setattr("app.pipeline.committer.settings.paperless_inbox_tag_id", 99)
    monkeypatch.setattr("app.pipeline.committer.settings.paperless_processed_tag_id", None)
    monkeypatch.setattr("app.pipeline.committer.settings.keep_inbox_tag", False)

    # Document currently has inbox tag (99) and another tag (5)
    mock_paperless.get_document.return_value = PaperlessDocument(id=42, title="old", tags=[99, 5])

    suggestion = _make_suggestion()
    decision = _make_decision()

    await commit_suggestion(suggestion, decision, mock_paperless)

    # Verify patch was called
    mock_paperless.patch_document.assert_called_once()
    call_args = mock_paperless.patch_document.call_args
    doc_id = call_args[0][0]
    fields = call_args[0][1]

    assert doc_id == 42
    assert fields["title"] == "Stromrechnung März 2024"
    assert fields["created_date"] == "2024-03-15"
    assert fields["correspondent"] == 5
    assert fields["document_type"] == 3
    # Tags: inbox (99) removed (keep_inbox_tag=False), existing (5) kept, new (20, 21) added
    assert set(fields["tags"]) == {5, 20, 21}


@pytest.mark.asyncio
async def test_commit_adds_processed_tag(mock_paperless, patch_db, monkeypatch):
    """When PAPERLESS_PROCESSED_TAG_ID is set, it should be added."""
    monkeypatch.setattr("app.pipeline.committer.settings.paperless_inbox_tag_id", 99)
    monkeypatch.setattr("app.pipeline.committer.settings.paperless_processed_tag_id", 77)
    monkeypatch.setattr("app.pipeline.committer.settings.keep_inbox_tag", False)

    mock_paperless.get_document.return_value = PaperlessDocument(id=42, title="old", tags=[99])

    suggestion = _make_suggestion()
    decision = _make_decision(tag_ids=[])

    await commit_suggestion(suggestion, decision, mock_paperless)

    fields = mock_paperless.patch_document.call_args[0][1]
    assert 77 in fields["tags"]
    assert 99 not in fields["tags"]


@pytest.mark.asyncio
async def test_commit_keeps_inbox_tag_when_enabled(mock_paperless, patch_db, monkeypatch):
    """When KEEP_INBOX_TAG=true (default), the inbox tag stays on the document."""
    monkeypatch.setattr("app.pipeline.committer.settings.paperless_inbox_tag_id", 99)
    monkeypatch.setattr("app.pipeline.committer.settings.paperless_processed_tag_id", None)
    monkeypatch.setattr("app.pipeline.committer.settings.keep_inbox_tag", True)

    mock_paperless.get_document.return_value = PaperlessDocument(id=42, title="old", tags=[99, 5])

    suggestion = _make_suggestion()
    decision = _make_decision()

    await commit_suggestion(suggestion, decision, mock_paperless)

    fields = mock_paperless.patch_document.call_args[0][1]
    # Tags: inbox (99) kept, existing (5) kept, new (20, 21) added
    assert set(fields["tags"]) == {5, 20, 21, 99}


@pytest.mark.asyncio
async def test_commit_skips_none_fields(mock_paperless, patch_db, monkeypatch):
    """None values for optional fields should not be sent."""
    monkeypatch.setattr("app.pipeline.committer.settings.paperless_inbox_tag_id", 99)
    monkeypatch.setattr("app.pipeline.committer.settings.paperless_processed_tag_id", None)

    mock_paperless.get_document.return_value = PaperlessDocument(id=42, title="old", tags=[99])

    suggestion = _make_suggestion()
    decision = _make_decision(
        date=None,
        correspondent_id=None,
        doctype_id=None,
        storage_path_id=None,
    )

    await commit_suggestion(suggestion, decision, mock_paperless)

    fields = mock_paperless.patch_document.call_args[0][1]
    assert "created_date" not in fields
    assert "correspondent" not in fields
    assert "document_type" not in fields
    assert "storage_path" not in fields


@pytest.mark.asyncio
async def test_commit_handles_paperless_error(mock_paperless, patch_db, monkeypatch):
    """On Paperless API error, exception is swallowed and error recorded."""
    monkeypatch.setattr("app.pipeline.committer.settings.paperless_inbox_tag_id", 99)
    monkeypatch.setattr("app.pipeline.committer.settings.paperless_processed_tag_id", None)

    mock_paperless.get_document.side_effect = Exception("Connection refused")

    suggestion = _make_suggestion()
    decision = _make_decision()

    # Should not raise
    await commit_suggestion(suggestion, decision, mock_paperless)

    # Patch should NOT have been called (failed before)
    mock_paperless.patch_document.assert_not_called()
