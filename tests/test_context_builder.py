"""Tests for the context builder: inbox filtering and overfetch compensation."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.config import settings
from app.models import PaperlessDocument, PaperlessEntity
from app.pipeline.context_builder import (
    document_summary,
    find_similar_by_id,
    find_similar_documents,
    find_similar_with_distances,
    find_similar_with_precomputed_embedding,
    store_embedding,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
INBOX_TAG_ID = settings.paperless_inbox_tag_id


def _make_doc(doc_id: int, *, inbox: bool = False, **kwargs) -> PaperlessDocument:
    """Create a test document, optionally in the inbox."""
    tags = [INBOX_TAG_ID] if inbox else []
    tags.extend(kwargs.pop("extra_tags", []))
    return PaperlessDocument(
        id=doc_id,
        title=kwargs.get("title", f"Doc {doc_id}"),
        content=kwargs.get("content", f"Content of doc {doc_id}"),
        correspondent=kwargs.get("correspondent"),
        document_type=kwargs.get("document_type"),
        storage_path=kwargs.get("storage_path"),
        tags=tags,
    )


def _mock_db_rows(doc_ids: list[int]):
    """Create a mock get_conn that returns the given document IDs from an embedding query."""
    rows = [{"document_id": did, "distance": 0.1 * (i + 1)} for i, did in enumerate(doc_ids)]

    mock_conn = MagicMock()
    mock_conn.execute.return_value.fetchall.return_value = rows
    mock_conn.__enter__ = MagicMock(return_value=mock_conn)
    mock_conn.__exit__ = MagicMock(return_value=False)

    return MagicMock(return_value=mock_conn)


def _mock_db_rows_with_distance(doc_ids_distances: list[tuple[int, float]]):
    """Create a mock get_conn returning document IDs with distance scores."""
    rows = [{"document_id": did, "distance": dist} for did, dist in doc_ids_distances]

    mock_conn = MagicMock()
    mock_conn.execute.return_value.fetchall.return_value = rows
    mock_conn.__enter__ = MagicMock(return_value=mock_conn)
    mock_conn.__exit__ = MagicMock(return_value=False)

    return MagicMock(return_value=mock_conn)


# ---------------------------------------------------------------------------
# Inbox filter tests
# ---------------------------------------------------------------------------
class TestInboxFilter:
    @pytest.mark.asyncio
    async def test_inbox_docs_excluded(self, mock_ollama: AsyncMock):
        """Documents with the inbox tag should be filtered out of context."""
        classified_doc = _make_doc(10, correspondent=2, document_type=10)
        inbox_doc = _make_doc(20, inbox=True)
        target = _make_doc(42, inbox=True)

        paperless = AsyncMock()
        paperless.get_document = AsyncMock(
            side_effect=lambda doc_id: {10: classified_doc, 20: inbox_doc}[doc_id]
        )

        with patch("app.pipeline.context_builder.get_conn", _mock_db_rows([10, 20])):
            result = await find_similar_documents(target, paperless, mock_ollama, limit=5)

        assert len(result) == 1
        assert result[0].id == 10

    @pytest.mark.asyncio
    async def test_non_inbox_docs_included(self, mock_ollama: AsyncMock):
        """Documents without the inbox tag should be included as context."""
        doc_a = _make_doc(10, extra_tags=[20])
        doc_b = _make_doc(11, extra_tags=[21])
        target = _make_doc(42, inbox=True)

        paperless = AsyncMock()
        paperless.get_document = AsyncMock(
            side_effect=lambda doc_id: {10: doc_a, 11: doc_b}[doc_id]
        )

        with patch("app.pipeline.context_builder.get_conn", _mock_db_rows([10, 11])):
            result = await find_similar_documents(target, paperless, mock_ollama, limit=5)

        assert len(result) == 2
        assert [d.id for d in result] == [10, 11]

    @pytest.mark.asyncio
    async def test_all_inbox_returns_empty(self, mock_ollama: AsyncMock):
        """If all candidates are in the inbox, return empty list."""
        inbox_a = _make_doc(10, inbox=True)
        inbox_b = _make_doc(11, inbox=True)
        target = _make_doc(42, inbox=True)

        paperless = AsyncMock()
        paperless.get_document = AsyncMock(
            side_effect=lambda doc_id: {10: inbox_a, 11: inbox_b}[doc_id]
        )

        with patch("app.pipeline.context_builder.get_conn", _mock_db_rows([10, 11])):
            result = await find_similar_documents(target, paperless, mock_ollama, limit=5)

        assert result == []


# ---------------------------------------------------------------------------
# Overfetch compensation
# ---------------------------------------------------------------------------
class TestOverfetchCompensation:
    @pytest.mark.asyncio
    async def test_limit_respected_despite_filtering(self, mock_ollama: AsyncMock):
        """Even when some candidates are filtered, the result should not exceed limit."""
        docs = {
            1: _make_doc(1),
            2: _make_doc(2, inbox=True),
            3: _make_doc(3),
            4: _make_doc(4, inbox=True),
            5: _make_doc(5),
            6: _make_doc(6),
        }
        target = _make_doc(42, inbox=True)

        paperless = AsyncMock()
        paperless.get_document = AsyncMock(side_effect=lambda doc_id: docs[doc_id])

        with patch(
            "app.pipeline.context_builder.get_conn",
            _mock_db_rows([1, 2, 3, 4, 5, 6]),
        ):
            result = await find_similar_documents(target, paperless, mock_ollama, limit=3)

        # 2 and 4 are inbox → filtered. Remaining: 1, 3, 5, 6. Limit=3 → [1, 3, 5]
        assert len(result) == 3
        assert all(INBOX_TAG_ID not in d.tags for d in result)

    @pytest.mark.asyncio
    async def test_fewer_than_limit_when_not_enough_candidates(self, mock_ollama: AsyncMock):
        """If there aren't enough non-inbox candidates, return what we have."""
        docs = {
            1: _make_doc(1),
            2: _make_doc(2, inbox=True),
        }
        target = _make_doc(42, inbox=True)

        paperless = AsyncMock()
        paperless.get_document = AsyncMock(side_effect=lambda doc_id: docs[doc_id])

        with patch("app.pipeline.context_builder.get_conn", _mock_db_rows([1, 2])):
            result = await find_similar_documents(target, paperless, mock_ollama, limit=5)

        assert len(result) == 1
        assert result[0].id == 1


# ---------------------------------------------------------------------------
# Full prompt integration: context metadata + inbox filter
# ---------------------------------------------------------------------------
class TestFullPromptWithContext:
    def test_mixed_context_docs_in_prompt(
        self,
        sample_doc: PaperlessDocument,
        sample_correspondents: list[PaperlessEntity],
        sample_doctypes: list[PaperlessEntity],
        sample_storage_paths: list[PaperlessEntity],
        sample_tags: list[PaperlessEntity],
    ):
        """Multiple context docs with varying metadata produce correct prompt."""
        from app.pipeline.classifier import build_user_prompt

        full_doc = PaperlessDocument(
            id=5,
            title="Stromrechnung Q1",
            content="Rechnung Stadtwerke",
            created_date="2024-03-15",
            correspondent=2,
            document_type=10,
            storage_path=30,
            tags=[20, 22],
        )
        partial_doc = PaperlessDocument(
            id=6,
            title="Brief von Max",
            content="Sehr geehrter Herr...",
            correspondent=1,
            document_type=None,
            tags=[],
        )

        prompt = build_user_prompt(
            target=sample_doc,
            context_docs=[full_doc, partial_doc],
            correspondents=sample_correspondents,
            doctypes=sample_doctypes,
            storage_paths=sample_storage_paths,
            tags=sample_tags,
        )

        # Header shows count
        assert "2 aehnliche bereits klassifizierte Dokumente" in prompt

        # Full doc has all metadata
        assert "Korrespondent: Stadtwerke München" in prompt
        assert "Dokumenttyp: Rechnung" in prompt
        assert "Speicherpfad: Finanzen/Rechnungen" in prompt
        assert "Tags: Finanzen, Strom" in prompt
        assert "Datum: 2024-03-15" in prompt

        # Partial doc has only correspondent
        assert "Korrespondent: Max Mustermann" in prompt

        # Target section is clean
        target_section = prompt.split("# Zu klassifizierendes Dokument")[1]
        assert "Korrespondent:" not in target_section
        assert "Dokumenttyp:" not in target_section
        assert "Speicherpfad:" not in target_section

    @pytest.mark.asyncio
    async def test_empty_content_doc_skipped_by_find_similar(self, mock_ollama: AsyncMock):
        """A target doc with empty content should return no context."""
        target = PaperlessDocument(id=1, title="", content="")
        paperless = AsyncMock()
        result = await find_similar_documents(target, paperless, mock_ollama, limit=5)
        assert result == []
        mock_ollama.embed.assert_not_called()


# ---------------------------------------------------------------------------
# find_similar_with_distances
# ---------------------------------------------------------------------------
class TestFindSimilarWithDistances:
    @pytest.mark.asyncio
    async def test_returns_distance_scores(self, mock_ollama: AsyncMock):
        """Results should include distance scores from the KNN query."""
        doc_a = _make_doc(10)
        doc_b = _make_doc(20)
        target = _make_doc(42, inbox=True)

        paperless = AsyncMock()
        paperless.get_document = AsyncMock(
            side_effect=lambda doc_id: {10: doc_a, 20: doc_b}[doc_id]
        )

        with patch(
            "app.pipeline.context_builder.get_conn",
            _mock_db_rows_with_distance([(10, 0.15), (20, 0.42)]),
        ):
            results = await find_similar_with_distances(target, paperless, mock_ollama, limit=5)

        assert len(results) == 2
        assert results[0].document.id == 10
        assert results[0].distance == 0.15
        assert results[1].document.id == 20
        assert results[1].distance == 0.42

    @pytest.mark.asyncio
    async def test_inbox_filter_with_distances(self, mock_ollama: AsyncMock):
        """Inbox docs should be excluded, preserving distances for kept docs."""
        classified = _make_doc(10)
        inbox_doc = _make_doc(20, inbox=True)
        target = _make_doc(42, inbox=True)

        paperless = AsyncMock()
        paperless.get_document = AsyncMock(
            side_effect=lambda doc_id: {10: classified, 20: inbox_doc}[doc_id]
        )

        with patch(
            "app.pipeline.context_builder.get_conn",
            _mock_db_rows_with_distance([(10, 0.2), (20, 0.3)]),
        ):
            results = await find_similar_with_distances(target, paperless, mock_ollama, limit=5)

        assert len(results) == 1
        assert results[0].document.id == 10
        assert results[0].distance == 0.2

    @pytest.mark.asyncio
    async def test_delegates_to_find_similar_documents(self, mock_ollama: AsyncMock):
        """find_similar_documents should return the same docs (without distances)."""
        doc_a = _make_doc(10)
        target = _make_doc(42, inbox=True)

        paperless = AsyncMock()
        paperless.get_document = AsyncMock(return_value=doc_a)

        with patch(
            "app.pipeline.context_builder.get_conn",
            _mock_db_rows_with_distance([(10, 0.25)]),
        ):
            result = await find_similar_documents(target, paperless, mock_ollama, limit=5)

        assert len(result) == 1
        assert result[0].id == 10
        # Result is PaperlessDocument, not SimilarDocument
        assert not hasattr(result[0], "distance")


# ---------------------------------------------------------------------------
# find_similar_by_id
# ---------------------------------------------------------------------------
class TestFindSimilarById:
    def test_returns_empty_for_unknown_doc(self):
        """If the document has no embedding, return empty list."""
        mock_conn = MagicMock()
        mock_conn.execute.return_value.fetchone.return_value = None
        mock_conn.__enter__ = MagicMock(return_value=mock_conn)
        mock_conn.__exit__ = MagicMock(return_value=False)

        with patch(
            "app.pipeline.context_builder.get_conn",
            MagicMock(return_value=mock_conn),
        ):
            result = find_similar_by_id(999, limit=5)

        assert result == []

    def test_returns_id_distance_pairs(self):
        """Should return (doc_id, distance) tuples excluding source doc."""
        mock_conn = MagicMock()

        # First call: fetch the embedding for the source doc
        embedding_row = {"embedding": b"\x00" * 768 * 4}
        # Second call: KNN results
        knn_rows = [
            {"document_id": 42, "distance": 0.0},  # self — should be excluded
            {"document_id": 10, "distance": 0.15},
            {"document_id": 20, "distance": 0.32},
        ]
        mock_conn.execute.side_effect = [
            MagicMock(fetchone=MagicMock(return_value=embedding_row)),
            MagicMock(fetchall=MagicMock(return_value=knn_rows)),
        ]
        mock_conn.__enter__ = MagicMock(return_value=mock_conn)
        mock_conn.__exit__ = MagicMock(return_value=False)

        with patch(
            "app.pipeline.context_builder.get_conn",
            MagicMock(return_value=mock_conn),
        ):
            result = find_similar_by_id(42, limit=5)

        assert result == [(10, 0.15), (20, 0.32)]

    def test_respects_limit(self):
        """Should not return more than `limit` results."""
        mock_conn = MagicMock()

        embedding_row = {"embedding": b"\x00" * 768 * 4}
        knn_rows = [
            {"document_id": 42, "distance": 0.0},
            {"document_id": 10, "distance": 0.1},
            {"document_id": 20, "distance": 0.2},
            {"document_id": 30, "distance": 0.3},
        ]
        mock_conn.execute.side_effect = [
            MagicMock(fetchone=MagicMock(return_value=embedding_row)),
            MagicMock(fetchall=MagicMock(return_value=knn_rows)),
        ]
        mock_conn.__enter__ = MagicMock(return_value=mock_conn)
        mock_conn.__exit__ = MagicMock(return_value=False)

        with patch(
            "app.pipeline.context_builder.get_conn",
            MagicMock(return_value=mock_conn),
        ):
            result = find_similar_by_id(42, limit=2)

        assert len(result) == 2


# ---------------------------------------------------------------------------
# find_similar_with_precomputed_embedding
# ---------------------------------------------------------------------------
class TestFindSimilarWithPrecomputedEmbedding:
    @pytest.mark.asyncio
    async def test_uses_precomputed_embedding(self):
        """Should use the provided embedding without calling ollama.embed()."""
        doc_a = _make_doc(10)
        target = _make_doc(42, inbox=True)
        embedding = [0.1] * 768

        paperless = AsyncMock()
        paperless.get_document = AsyncMock(return_value=doc_a)

        with patch(
            "app.pipeline.context_builder.get_conn",
            _mock_db_rows_with_distance([(10, 0.25)]),
        ):
            results = await find_similar_with_precomputed_embedding(
                target, embedding, paperless, limit=5
            )

        assert len(results) == 1
        assert results[0].document.id == 10
        assert results[0].distance == 0.25

    @pytest.mark.asyncio
    async def test_filters_inbox_docs(self):
        """Inbox docs should be excluded just like in find_similar_with_distances."""
        classified = _make_doc(10)
        inbox_doc = _make_doc(20, inbox=True)
        target = _make_doc(42, inbox=True)
        embedding = [0.1] * 768

        paperless = AsyncMock()
        paperless.get_document = AsyncMock(
            side_effect=lambda doc_id: {10: classified, 20: inbox_doc}[doc_id]
        )

        with patch(
            "app.pipeline.context_builder.get_conn",
            _mock_db_rows_with_distance([(10, 0.2), (20, 0.3)]),
        ):
            results = await find_similar_with_precomputed_embedding(
                target, embedding, paperless, limit=5
            )

        assert len(results) == 1
        assert results[0].document.id == 10

    @pytest.mark.asyncio
    async def test_excludes_self(self):
        """The source document should be excluded from results."""
        other_doc = _make_doc(10)
        target = _make_doc(42, inbox=True)
        embedding = [0.1] * 768

        paperless = AsyncMock()
        paperless.get_document = AsyncMock(return_value=other_doc)

        with patch(
            "app.pipeline.context_builder.get_conn",
            _mock_db_rows_with_distance([(42, 0.0), (10, 0.3)]),
        ):
            results = await find_similar_with_precomputed_embedding(
                target, embedding, paperless, limit=5
            )

        assert len(results) == 1
        assert results[0].document.id == 10


# ---------------------------------------------------------------------------
# store_embedding
# ---------------------------------------------------------------------------
class TestStoreEmbedding:
    def test_writes_to_both_tables(self):
        """store_embedding should write to doc_embeddings and doc_embedding_meta."""
        doc = PaperlessDocument(
            id=42,
            title="Test Doc",
            content="content",
            correspondent=2,
            document_type=10,
            tags=[99],
        )
        embedding = [0.1] * 768

        mock_conn = MagicMock()
        mock_conn.__enter__ = MagicMock(return_value=mock_conn)
        mock_conn.__exit__ = MagicMock(return_value=False)

        with patch(
            "app.pipeline.context_builder.get_conn",
            MagicMock(return_value=mock_conn),
        ):
            store_embedding(doc, embedding)

        # Should have executed 2 SQL statements
        assert mock_conn.execute.call_count == 2
        # First call: doc_embeddings INSERT
        first_sql = mock_conn.execute.call_args_list[0][0][0]
        assert "doc_embeddings" in first_sql
        # Second call: doc_embedding_meta INSERT
        second_sql = mock_conn.execute.call_args_list[1][0][0]
        assert "doc_embedding_meta" in second_sql


# ---------------------------------------------------------------------------
# document_summary
# ---------------------------------------------------------------------------
class TestDocumentSummary:
    def test_title_and_content(self):
        """Should include title and truncated content."""
        doc = _make_doc(1, title="My Title", content="My Content")
        result = document_summary(doc)
        assert "My Title" in result
        assert "My Content" in result

    def test_content_truncated_at_1000(self):
        """Content should be limited to 1000 chars."""
        doc = _make_doc(1, content="x" * 2000)
        result = document_summary(doc)
        # Title + newline + 1000 chars of content
        assert len(result) <= len("Doc 1") + 1 + 1000

    def test_empty_content(self):
        """A doc with only a title should still return the title."""
        doc = PaperlessDocument(id=1, title="Title Only", content="", tags=[])
        result = document_summary(doc)
        assert result == "Title Only"

    def test_empty_title_and_content(self):
        """A doc with no title and no content should return empty string."""
        doc = PaperlessDocument(id=1, title="", content="", tags=[])
        result = document_summary(doc)
        assert result == ""
