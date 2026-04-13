"""Tests for worker entity resolution, tag handling, and poll cycle logging."""

from __future__ import annotations

from datetime import UTC, datetime
from unittest.mock import AsyncMock, patch

import pytest
import structlog

from app.models import PaperlessDocument
from app.worker import (
    _phase_classify,
    _phase_embed,
    _phase_ocr,
    _process_document,
    _resolve_entity,
    _resolve_tags,
    poll_inbox,
)


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


# ---------------------------------------------------------------------------
# Phased pipeline tests
# ---------------------------------------------------------------------------
def _make_doc(doc_id: int, **kwargs) -> PaperlessDocument:
    return PaperlessDocument(
        id=doc_id,
        title=kwargs.get("title", f"Doc {doc_id}"),
        content=kwargs.get("content", f"Content of doc {doc_id}"),
        tags=kwargs.get("tags", [99]),
    )


class TestPhaseEmbed:
    """Tests for the embedding phase."""

    @pytest.mark.asyncio
    async def test_embed_called_once_per_doc(self):
        """Each document should be embedded exactly once (not twice like before)."""
        docs = [_make_doc(1), _make_doc(2), _make_doc(3)]
        mock_ollama = AsyncMock()
        mock_ollama.embed = AsyncMock(return_value=[0.1] * 768)
        mock_ollama.embed_model = "nomic-embed-text-v2-moe"
        mock_ollama.unload_model = AsyncMock()
        mock_paperless = AsyncMock()
        # No similar docs in DB
        with patch("app.pipeline.context_builder.get_conn") as mock_conn:
            mock_ctx = mock_conn.return_value.__enter__.return_value
            mock_ctx.execute.return_value.fetchall.return_value = []
            results = await _phase_embed(docs, mock_paperless, mock_ollama)

        # Exactly 3 embed calls — one per doc
        assert mock_ollama.embed.call_count == 3
        assert len(results) == 3

    @pytest.mark.asyncio
    async def test_embed_failure_produces_empty_result(self):
        """If embedding fails for a doc, it gets None embedding + empty similar_results."""
        docs = [_make_doc(1)]
        mock_ollama = AsyncMock()
        mock_ollama.embed = AsyncMock(side_effect=RuntimeError("model unavailable"))
        mock_ollama.embed_model = "nomic-embed-text-v2-moe"
        mock_ollama.unload_model = AsyncMock()

        results = await _phase_embed(docs, AsyncMock(), mock_ollama)

        assert results[1].embedding is None
        assert results[1].similar_results == []

    @pytest.mark.asyncio
    async def test_unload_called_after_embed_phase(self):
        """The embed model should be unloaded at the end of the phase."""
        mock_ollama = AsyncMock()
        mock_ollama.embed = AsyncMock(return_value=[0.1] * 768)
        mock_ollama.embed_model = "nomic-embed-text-v2-moe"
        mock_ollama.unload_model = AsyncMock()

        with patch("app.pipeline.context_builder.get_conn") as mock_conn:
            mock_ctx = mock_conn.return_value.__enter__.return_value
            mock_ctx.execute.return_value.fetchall.return_value = []
            await _phase_embed([_make_doc(1)], AsyncMock(), mock_ollama)

        mock_ollama.unload_model.assert_called_once_with("nomic-embed-text-v2-moe")


class TestPhaseOcr:
    """Tests for the OCR correction phase."""

    @pytest.mark.asyncio
    async def test_ocr_modifies_content(self):
        """OCR phase should update document content when corrections are made."""
        doc = _make_doc(1, content="broken text")
        mock_ollama = AsyncMock()
        mock_ollama.ocr_model = "gemma3:1b"
        mock_ollama.unload_model = AsyncMock()

        with (
            patch("app.worker.settings") as mock_settings,
            patch("app.worker.maybe_correct_ocr") as mock_ocr,
        ):
            mock_settings.enable_ocr_correction = True
            mock_ocr.return_value = ("fixed text", 3)
            result = await _phase_ocr([doc], mock_ollama)

        assert result[0].content == "fixed text"
        mock_ollama.unload_model.assert_called_once_with("gemma3:1b")

    @pytest.mark.asyncio
    async def test_ocr_skipped_when_disabled(self):
        """When OCR is disabled, the phase should return docs unchanged."""
        docs = [_make_doc(1), _make_doc(2)]
        mock_ollama = AsyncMock()
        mock_ollama.unload_model = AsyncMock()

        with patch("app.worker.settings") as mock_settings:
            mock_settings.enable_ocr_correction = False
            result = await _phase_ocr(docs, mock_ollama)

        assert result == docs
        mock_ollama.unload_model.assert_not_called()

    @pytest.mark.asyncio
    async def test_ocr_error_keeps_original_content(self):
        """If OCR fails for a doc, original content should be preserved."""
        doc = _make_doc(1, content="original text")
        mock_ollama = AsyncMock()
        mock_ollama.ocr_model = "gemma3:1b"
        mock_ollama.unload_model = AsyncMock()

        with (
            patch("app.worker.settings") as mock_settings,
            patch("app.worker.maybe_correct_ocr", side_effect=RuntimeError("fail")),
        ):
            mock_settings.enable_ocr_correction = True
            result = await _phase_ocr([doc], mock_ollama)

        assert result[0].content == "original text"


class TestPhaseClassify:
    """Tests for the classification phase."""

    @pytest.mark.asyncio
    async def test_classify_uses_precomputed_context(self):
        """Classification should use similar_results from the embedding phase."""
        from app.worker import _EmbeddingResult

        doc = _make_doc(1)
        context_doc = _make_doc(10, title="Similar Doc")
        from app.pipeline.context_builder import SimilarDocument

        embed_results = {
            1: _EmbeddingResult(
                embedding=[0.1] * 768,
                similar_results=[SimilarDocument(document=context_doc, distance=0.2)],
            )
        }

        mock_ollama = AsyncMock()
        mock_ollama.model = "gemma3:4b"
        mock_ollama.unload_model = AsyncMock()

        with (
            patch("app.worker.classifier.classify") as mock_classify,
            patch("app.worker._store_suggestion") as mock_store,
            patch("app.worker.notify_suggestion"),
            patch("app.worker.context_builder.store_embedding"),
            patch("app.worker.settings") as mock_settings,
        ):
            mock_settings.auto_commit_confidence = 0
            from app.models import ClassificationResult

            mock_classify.return_value = (
                ClassificationResult(
                    title="Test",
                    confidence=80,
                    reasoning="test",
                    tags=[],
                ),
                '{"title":"Test"}',
            )
            mock_store.return_value = AsyncMock(id=1, proposed_correspondent_id=None)

            _classified, _auto_committed, _errored = await _phase_classify(
                [doc], embed_results, AsyncMock(), mock_ollama, [], [], [], []
            )

        # Verify classify was called with the context doc from embedding phase
        classify_call_args = mock_classify.call_args
        context_docs_arg = classify_call_args[0][1]  # second positional arg
        assert len(context_docs_arg) == 1
        assert context_docs_arg[0].id == 10

    @pytest.mark.asyncio
    async def test_unload_called_after_classify_phase(self):
        """The classify model should be unloaded at the end of the phase."""
        from app.worker import _EmbeddingResult

        mock_ollama = AsyncMock()
        mock_ollama.model = "gemma3:4b"
        mock_ollama.unload_model = AsyncMock()

        with (
            patch("app.worker.classifier.classify") as mock_classify,
            patch("app.worker._store_suggestion") as mock_store,
            patch("app.worker.notify_suggestion"),
            patch("app.worker.context_builder.store_embedding"),
            patch("app.worker.settings") as mock_settings,
        ):
            mock_settings.auto_commit_confidence = 0
            from app.models import ClassificationResult

            mock_classify.return_value = (
                ClassificationResult(title="Test", confidence=80, reasoning="test", tags=[]),
                "{}",
            )
            mock_store.return_value = AsyncMock(id=1, proposed_correspondent_id=None)

            await _phase_classify(
                [_make_doc(1)],
                {1: _EmbeddingResult()},
                AsyncMock(),
                mock_ollama,
                [],
                [],
                [],
                [],
            )

        mock_ollama.unload_model.assert_called_once_with("gemma3:4b")

    @pytest.mark.asyncio
    async def test_embed_still_indexed_on_classify_failure(self):
        """Even if classification fails, a pre-computed embedding should be stored."""
        from app.worker import _EmbeddingResult

        doc = _make_doc(1)
        embed_results = {1: _EmbeddingResult(embedding=[0.1] * 768, similar_results=[])}

        mock_ollama = AsyncMock()
        mock_ollama.model = "gemma3:4b"
        mock_ollama.unload_model = AsyncMock()

        with (
            patch("app.worker.classifier.classify", side_effect=RuntimeError("LLM failed")),
            patch("app.worker._write_error"),
            patch("app.worker.context_builder.store_embedding") as mock_store_emb,
            patch("app.worker.settings") as mock_settings,
        ):
            mock_settings.auto_commit_confidence = 0
            _classified, _auto_committed, errored = await _phase_classify(
                [doc], embed_results, AsyncMock(), mock_ollama, [], [], [], []
            )

        assert errored == 1
        # Embedding should still be stored despite classification failure
        mock_store_emb.assert_called_once_with(doc, [0.1] * 768)


class TestPhasedPollInbox:
    """Integration tests for the full phased poll_inbox flow."""

    @pytest.mark.asyncio
    async def test_all_embeds_before_all_classifies(self, patch_db, tmp_db):
        """All embed() calls should happen before any chat_json() calls."""
        docs = [_make_doc(1), _make_doc(2)]
        call_order: list[str] = []

        async def track_embed(text):
            call_order.append("embed")
            return [0.1] * 768

        async def track_chat_json(**kwargs):
            call_order.append("chat_json")
            return {
                "title": "Test",
                "date": None,
                "correspondent": None,
                "document_type": None,
                "storage_path": None,
                "tags": [],
                "confidence": 80,
                "reasoning": "test",
            }

        mock_ollama = AsyncMock()
        mock_ollama.embed = AsyncMock(side_effect=track_embed)
        mock_ollama.chat_json = AsyncMock(side_effect=track_chat_json)
        mock_ollama.model = "gemma3:4b"
        mock_ollama.embed_model = "nomic-embed-text-v2-moe"
        mock_ollama.ocr_model = "gemma3:1b"
        mock_ollama.unload_model = AsyncMock()

        mock_paperless = AsyncMock()
        mock_paperless.list_inbox_documents = AsyncMock(return_value=docs)
        mock_paperless.list_correspondents = AsyncMock(return_value=[])
        mock_paperless.list_document_types = AsyncMock(return_value=[])
        mock_paperless.list_storage_paths = AsyncMock(return_value=[])
        mock_paperless.list_tags = AsyncMock(return_value=[])

        with (
            patch("app.worker._paperless", mock_paperless),
            patch("app.worker._ollama", mock_ollama),
            patch("app.pipeline.context_builder.get_conn") as mock_conn,
            patch("app.worker.notify_suggestion"),
            patch("app.worker.context_builder.store_embedding"),
        ):
            mock_ctx = mock_conn.return_value.__enter__.return_value
            mock_ctx.execute.return_value.fetchall.return_value = []
            await poll_inbox()

        # Verify ordering: all embeds come before all chat_json calls
        embed_indices = [i for i, c in enumerate(call_order) if c == "embed"]
        chat_indices = [i for i, c in enumerate(call_order) if c == "chat_json"]
        if embed_indices and chat_indices:
            assert max(embed_indices) < min(chat_indices), (
                f"Embed calls should all precede chat_json calls. Order: {call_order}"
            )
