"""Tests for worker entity resolution, tag handling, and poll cycle logging."""

from __future__ import annotations

from datetime import UTC, datetime
from unittest.mock import AsyncMock, patch

import pytest
import structlog

from app.models import PaperlessDocument
from app.worker import _process_document, _resolve_entity, _resolve_tags, poll_inbox


class TestResolveEntity:
    def test_exact_match(self, sample_entities):
        assert _resolve_entity("Max Mustermann", sample_entities) == 1

    def test_case_insensitive(self, sample_entities):
        assert _resolve_entity("max mustermann", sample_entities) == 1
        assert _resolve_entity("STADTWERKE MÜNCHEN", sample_entities) == 2
        assert _resolve_entity("deutsche post", sample_entities) == 3

    def test_no_match(self, sample_entities):
        assert _resolve_entity("Unbekannter Absender", sample_entities) is None

    def test_none_input(self, sample_entities):
        assert _resolve_entity(None, sample_entities) is None

    def test_empty_string(self, sample_entities):
        assert _resolve_entity("", sample_entities) is None

    def test_empty_entity_list(self):
        assert _resolve_entity("Something", []) is None

    def test_partial_match_not_found(self, sample_entities):
        """Partial matches should NOT resolve — only exact."""
        assert _resolve_entity("Max", sample_entities) is None
        assert _resolve_entity("Stadtwerke", sample_entities) is None

    def test_whitespace_not_trimmed(self, sample_entities):
        """Leading/trailing whitespace means no match."""
        assert _resolve_entity(" Max Mustermann ", sample_entities) is None


class TestResolveTags:
    def test_all_tags_found(self, sample_entities, patch_db):
        proposed = [
            {"name": "Finanzen", "confidence": 90},
            {"name": "Wohnung", "confidence": 70},
        ]
        ids, dicts = _resolve_tags(proposed, sample_entities)
        assert ids == [20, 21]
        assert len(dicts) == 2
        assert dicts[0] == {"name": "Finanzen", "confidence": 90, "id": 20}
        assert dicts[1] == {"name": "Wohnung", "confidence": 70, "id": 21}

    def test_mixed_found_and_new(self, sample_entities, patch_db):
        proposed = [
            {"name": "Finanzen", "confidence": 90},
            {"name": "NeuerTag", "confidence": 60},
        ]
        ids, dicts = _resolve_tags(proposed, sample_entities)
        assert ids == [20]  # only Finanzen resolved
        assert dicts[1] == {"name": "NeuerTag", "confidence": 60, "id": None}

    def test_all_tags_new(self, sample_entities, patch_db):
        proposed = [
            {"name": "Komplett Neu", "confidence": 50},
        ]
        ids, dicts = _resolve_tags(proposed, sample_entities)
        assert ids == []
        assert dicts[0]["id"] is None

    def test_empty_proposed(self, sample_entities, patch_db):
        ids, dicts = _resolve_tags([], sample_entities)
        assert ids == []
        assert dicts == []

    def test_case_insensitive_tag_match(self, sample_entities, patch_db):
        proposed = [{"name": "finanzen", "confidence": 80}]
        ids, _dicts = _resolve_tags(proposed, sample_entities)
        assert ids == [20]

    def test_default_confidence(self, sample_entities, patch_db):
        proposed = [{"name": "Finanzen"}]  # no confidence key
        _ids, dicts = _resolve_tags(proposed, sample_entities)
        assert dicts[0]["confidence"] == 50  # default


class TestProcessDocumentReturn:
    """Verify _process_document returns the correct ProcessResult."""

    @pytest.mark.asyncio
    async def test_skipped_when_already_processed(self, patch_db, tmp_db):
        """A document already in processed_documents (matching timestamp, non-error) returns 'skipped'."""
        import sqlite3

        doc = PaperlessDocument(
            id=42,
            title="Test Doc",
            content="some text",
            modified=datetime(2024, 3, 15, 10, 0, 0, tzinfo=UTC),
            tags=[99],
        )
        # Pre-insert a matching processed_documents row
        conn = sqlite3.connect(str(tmp_db))
        conn.execute(
            "INSERT INTO processed_documents (document_id, last_updated_at, last_processed, status) "
            "VALUES (?, ?, datetime('now'), 'committed')",
            (42, doc.modified.isoformat()),
        )
        conn.commit()
        conn.close()

        result = await _process_document(
            doc,
            AsyncMock(),  # paperless (unused for skip path)
            AsyncMock(),  # ollama (unused for skip path)
            [],
            [],
            [],
            [],
        )
        assert result == "skipped"

    @pytest.mark.asyncio
    async def test_skipped_includes_status_in_debug_log(self, patch_db, tmp_db):
        """The debug log for skipped documents includes the stored status."""
        import sqlite3

        doc = PaperlessDocument(
            id=42,
            title="Test Doc",
            content="some text",
            modified=datetime(2024, 3, 15, 10, 0, 0, tzinfo=UTC),
            tags=[99],
        )
        conn = sqlite3.connect(str(tmp_db))
        conn.execute(
            "INSERT INTO processed_documents (document_id, last_updated_at, last_processed, status) "
            "VALUES (?, ?, datetime('now'), 'pending')",
            (42, doc.modified.isoformat()),
        )
        conn.commit()
        conn.close()

        with structlog.testing.capture_logs() as logs:
            result = await _process_document(
                doc,
                AsyncMock(),
                AsyncMock(),
                [],
                [],
                [],
                [],
            )

        assert result == "skipped"
        skip_logs = [entry for entry in logs if entry.get("event") == "document already processed"]
        assert len(skip_logs) == 1
        assert skip_logs[0]["status"] == "pending"
        assert skip_logs[0]["doc_id"] == 42


class TestPollCycleSummary:
    """Verify poll_inbox logs a summary with correct counters."""

    @pytest.mark.asyncio
    async def test_all_skipped_summary(self, patch_db, tmp_db, sample_doc):
        """When all docs are already processed, summary shows all skipped."""
        import sqlite3

        doc = sample_doc
        doc.modified = datetime(2024, 3, 15, 10, 0, 0, tzinfo=UTC)

        # Pre-insert as already processed
        conn = sqlite3.connect(str(tmp_db))
        conn.execute(
            "INSERT INTO processed_documents (document_id, last_updated_at, last_processed, status) "
            "VALUES (?, ?, datetime('now'), 'committed')",
            (doc.id, doc.modified.isoformat()),
        )
        conn.commit()
        conn.close()

        mock_paperless = AsyncMock()
        mock_paperless.list_inbox_documents = AsyncMock(return_value=[doc])
        mock_paperless.list_correspondents = AsyncMock(return_value=[])
        mock_paperless.list_document_types = AsyncMock(return_value=[])
        mock_paperless.list_storage_paths = AsyncMock(return_value=[])
        mock_paperless.list_tags = AsyncMock(return_value=[])

        with (
            patch("app.worker._paperless", mock_paperless),
            patch("app.worker._ollama", AsyncMock()),
            structlog.testing.capture_logs() as logs,
        ):
            await poll_inbox()

        summary = [entry for entry in logs if entry.get("event") == "poll cycle complete"]
        assert len(summary) == 1
        assert summary[0]["total"] == 1
        assert summary[0]["skipped"] == 1
        assert summary[0]["classified"] == 0
        assert summary[0]["auto_committed"] == 0
        assert summary[0]["errored"] == 0
