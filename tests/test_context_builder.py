"""Tests for the context builder: inbox filtering and similarity search via sqlite-vec."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.config import settings
from app.db import EMBED_DIM
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


# ---------------------------------------------------------------------------
# Inbox filter tests (now sqlite-vec based)
# ---------------------------------------------------------------------------
class TestInboxFilter:
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

        with patch("app.pipeline.context_builder._find_similar_ids") as mock_knn:
            mock_knn.return_value = [(10, 0.1), (11, 0.2)]
            result = await find_similar_documents(target, paperless, mock_ollama, limit=5)

        assert len(result) == 2
        assert [d.id for d in result] == [10, 11]

    @pytest.mark.asyncio
    async def test_empty_results(self, mock_ollama: AsyncMock):
        """If KNN returns no results, return empty list."""
        target = _make_doc(42, inbox=True)
        paperless = AsyncMock()

        with patch("app.pipeline.context_builder._find_similar_ids", return_value=[]):
            result = await find_similar_documents(target, paperless, mock_ollama, limit=5)

        assert result == []


# ---------------------------------------------------------------------------
# Overfetch compensation
# ---------------------------------------------------------------------------
class TestOverfetchCompensation:
    @pytest.mark.asyncio
    async def test_limit_respected(self, mock_ollama: AsyncMock):
        """Results should not exceed the requested limit."""
        docs = {
            1: _make_doc(1),
            3: _make_doc(3),
            5: _make_doc(5),
        }
        target = _make_doc(42, inbox=True)

        paperless = AsyncMock()
        paperless.get_document = AsyncMock(side_effect=lambda doc_id: docs[doc_id])

        with patch("app.pipeline.context_builder._find_similar_ids") as mock_knn:
            mock_knn.return_value = [(1, 0.1), (3, 0.2), (5, 0.3)]
            result = await find_similar_documents(target, paperless, mock_ollama, limit=3)

        assert len(result) == 3

    @pytest.mark.asyncio
    async def test_fewer_than_limit_when_not_enough_candidates(self, mock_ollama: AsyncMock):
        """If there aren't enough candidates, return what we have."""
        docs = {
            1: _make_doc(1),
        }
        target = _make_doc(42, inbox=True)

        paperless = AsyncMock()
        paperless.get_document = AsyncMock(side_effect=lambda doc_id: docs[doc_id])

        with patch("app.pipeline.context_builder._find_similar_ids") as mock_knn:
            mock_knn.return_value = [(1, 0.1)]
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
        """Results should include distance scores from the vector search."""
        doc_a = _make_doc(10)
        doc_b = _make_doc(20)
        target = _make_doc(42, inbox=True)

        paperless = AsyncMock()
        paperless.get_document = AsyncMock(
            side_effect=lambda doc_id: {10: doc_a, 20: doc_b}[doc_id]
        )

        with patch("app.pipeline.context_builder._find_similar_ids") as mock_knn:
            mock_knn.return_value = [(10, 0.15), (20, 0.42)]
            results = await find_similar_with_distances(target, paperless, mock_ollama, limit=5)

        assert len(results) == 2
        assert results[0].document.id == 10
        assert results[1].document.id == 20

    @pytest.mark.asyncio
    async def test_excludes_source_doc(self, mock_ollama: AsyncMock):
        """KNN search should exclude the source document."""
        classified = _make_doc(10)
        target = _make_doc(42, inbox=True)

        paperless = AsyncMock()
        paperless.get_document = AsyncMock(return_value=classified)

        with patch("app.pipeline.context_builder._find_similar_ids") as mock_knn:
            mock_knn.return_value = [(10, 0.2)]
            results = await find_similar_with_distances(target, paperless, mock_ollama, limit=5)

        assert len(results) == 1
        assert results[0].document.id == 10
        # Verify exclude_id was passed
        call_kwargs = mock_knn.call_args
        assert call_kwargs.kwargs.get("exclude_id") == 42

    @pytest.mark.asyncio
    async def test_delegates_to_find_similar_documents(self, mock_ollama: AsyncMock):
        """find_similar_documents should return the same docs (without distances)."""
        doc_a = _make_doc(10)
        target = _make_doc(42, inbox=True)

        paperless = AsyncMock()
        paperless.get_document = AsyncMock(return_value=doc_a)

        with patch("app.pipeline.context_builder._find_similar_ids") as mock_knn:
            mock_knn.return_value = [(10, 0.25)]
            result = await find_similar_documents(target, paperless, mock_ollama, limit=5)

        assert len(result) == 1
        assert result[0].id == 10
        # Result is PaperlessDocument, not SimilarDocument
        assert not hasattr(result[0], "distance")


# ---------------------------------------------------------------------------
# find_similar_by_id (synchronous, sqlite-vec based)
# ---------------------------------------------------------------------------
class TestFindSimilarById:
    def test_returns_empty_for_unknown_doc(self):
        """If the document has no embedding, return empty list."""
        mock_conn = MagicMock()
        mock_conn.__enter__ = MagicMock(return_value=mock_conn)
        mock_conn.__exit__ = MagicMock(return_value=False)
        mock_conn.execute.return_value.fetchone.return_value = None

        with patch("app.pipeline.context_builder.get_conn", return_value=mock_conn):
            result = find_similar_by_id(999, limit=5)

        assert result == []

    def test_returns_id_distance_pairs(self):
        """Should return (doc_id, distance) tuples excluding source doc."""
        import struct

        fake_embedding = struct.pack(f"{EMBED_DIM}f", *([0.1] * EMBED_DIM))
        mock_conn = MagicMock()
        mock_conn.__enter__ = MagicMock(return_value=mock_conn)
        mock_conn.__exit__ = MagicMock(return_value=False)

        # First call: get stored embedding
        fetch_row = {"embedding": fake_embedding}
        # Second call: KNN search
        knn_rows = [
            {"document_id": 10, "distance": 0.15},
            {"document_id": 20, "distance": 0.32},
        ]

        call_count = 0

        def mock_execute(sql, params=None):
            nonlocal call_count
            call_count += 1
            result = MagicMock()
            if call_count == 1:
                result.fetchone.return_value = fetch_row
            else:
                result.fetchall.return_value = knn_rows
            return result

        mock_conn.execute = mock_execute

        with patch("app.pipeline.context_builder.get_conn", return_value=mock_conn):
            result = find_similar_by_id(42, limit=5)

        assert len(result) == 2
        assert result[0][0] == 10
        assert result[1][0] == 20


# ---------------------------------------------------------------------------
# find_similar_with_precomputed_embedding
# ---------------------------------------------------------------------------
class TestFindSimilarWithPrecomputedEmbedding:
    @pytest.mark.asyncio
    async def test_uses_precomputed_embedding(self):
        """Should use the provided embedding without calling ollama.embed()."""
        doc_a = _make_doc(10)
        target = _make_doc(42, inbox=True)
        embedding = [0.1] * EMBED_DIM

        paperless = AsyncMock()
        paperless.get_document = AsyncMock(return_value=doc_a)

        with patch("app.pipeline.context_builder._find_similar_ids") as mock_knn:
            mock_knn.return_value = [(10, 0.25)]

            results = await find_similar_with_precomputed_embedding(
                target, embedding, paperless, limit=5
            )

        assert len(results) == 1
        assert results[0].document.id == 10
        assert results[0].distance == pytest.approx(0.25)
        mock_knn.assert_called_once()

    @pytest.mark.asyncio
    async def test_excludes_self(self):
        """exclude_id should be passed to the KNN search."""
        doc_a = _make_doc(10)
        target = _make_doc(42, inbox=True)
        embedding = [0.1] * EMBED_DIM

        paperless = AsyncMock()
        paperless.get_document = AsyncMock(return_value=doc_a)

        with patch("app.pipeline.context_builder._find_similar_ids") as mock_knn:
            mock_knn.return_value = [(10, 0.2)]

            await find_similar_with_precomputed_embedding(target, embedding, paperless, limit=5)

        call_kwargs = mock_knn.call_args
        assert call_kwargs.kwargs.get("exclude_id") == 42

    @pytest.mark.asyncio
    async def test_empty_results(self):
        """Should return empty list when KNN returns no hits."""
        target = _make_doc(42, inbox=True)
        embedding = [0.1] * EMBED_DIM

        paperless = AsyncMock()

        with patch("app.pipeline.context_builder._find_similar_ids", return_value=[]):
            results = await find_similar_with_precomputed_embedding(
                target, embedding, paperless, limit=5
            )

        assert results == []


# ---------------------------------------------------------------------------
# store_embedding
# ---------------------------------------------------------------------------
class TestStoreEmbedding:
    def test_writes_to_sqlite_meta_and_fts(self):
        """store_embedding should write to doc_embeddings, doc_embedding_meta, and doc_fts."""
        doc = PaperlessDocument(
            id=42,
            title="Test Doc",
            content="content",
            correspondent=2,
            document_type=10,
            tags=[99],
        )
        embedding = [0.1] * EMBED_DIM

        mock_conn = MagicMock()
        mock_conn.__enter__ = MagicMock(return_value=mock_conn)
        mock_conn.__exit__ = MagicMock(return_value=False)

        with patch(
            "app.pipeline.context_builder.get_conn",
            MagicMock(return_value=mock_conn),
        ):
            store_embedding(doc, embedding)

        # 5 SQL executions: vec0 delete + vec0 insert + meta + FTS delete + FTS insert
        assert mock_conn.execute.call_count == 5
        embed_del_sql = mock_conn.execute.call_args_list[0][0][0]
        assert "DELETE FROM doc_embeddings" in embed_del_sql
        embed_ins_sql = mock_conn.execute.call_args_list[1][0][0]
        assert "INSERT INTO doc_embeddings" in embed_ins_sql
        meta_sql = mock_conn.execute.call_args_list[2][0][0]
        assert "doc_embedding_meta" in meta_sql
        fts_del_sql = mock_conn.execute.call_args_list[3][0][0]
        assert "DELETE FROM doc_fts" in fts_del_sql
        fts_ins_sql = mock_conn.execute.call_args_list[4][0][0]
        assert "INSERT INTO doc_fts" in fts_ins_sql


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

    def test_content_truncated_at_default(self):
        """Total output should be limited to embed_max_chars (default 6000)."""
        doc = _make_doc(1, content="x" * 10000)
        result = document_summary(doc)
        assert len(result) <= 6000

    def test_content_truncated_at_custom_limit(self, monkeypatch):
        """Total truncation should respect a custom embed_max_chars value."""
        monkeypatch.setattr("app.pipeline.context_builder.settings.embed_max_chars", 500)
        doc = _make_doc(1, content="x" * 2000)
        result = document_summary(doc)
        assert len(result) <= 500

    def test_long_title_still_truncated(self, monkeypatch):
        """A long title + content combo must not exceed embed_max_chars."""
        monkeypatch.setattr("app.pipeline.context_builder.settings.embed_max_chars", 500)
        doc = PaperlessDocument(id=1, title="T" * 300, content="C" * 400, tags=[])
        result = document_summary(doc)
        assert len(result) <= 500
        assert result.startswith("T")

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


# ---------------------------------------------------------------------------
# Reciprocal Rank Fusion
# ---------------------------------------------------------------------------
class TestReciprocalRankFusion:
    def test_basic_rrf(self):
        """Documents appearing in both lists should rank highest."""
        from app.pipeline.context_builder import _reciprocal_rank_fusion

        vec_hits = [(1, 0.1), (2, 0.2), (3, 0.3)]
        fts_hits = [(2, 5.0), (4, 3.0), (1, 1.0)]

        fused = _reciprocal_rank_fusion(vec_hits, fts_hits, vector_weight=0.5)

        # Doc 2 is rank 2 in vec, rank 1 in FTS — should be top
        # Doc 1 is rank 1 in vec, rank 3 in FTS — should be second
        ids = [doc_id for doc_id, _ in fused]
        assert ids[0] == 2
        assert ids[1] == 1
        assert len(fused) == 4  # all unique docs

    def test_empty_lists(self):
        """Empty inputs should produce empty output."""
        from app.pipeline.context_builder import _reciprocal_rank_fusion

        assert _reciprocal_rank_fusion([], []) == []

    def test_vector_only(self):
        """When FTS is empty, only vector results appear."""
        from app.pipeline.context_builder import _reciprocal_rank_fusion

        vec_hits = [(1, 0.1), (2, 0.2)]
        fused = _reciprocal_rank_fusion(vec_hits, [], vector_weight=0.7)
        ids = [doc_id for doc_id, _ in fused]
        assert 1 in ids
        assert 2 in ids

    def test_weight_bias(self):
        """vector_weight=1.0 should fully favour vector ranking."""
        from app.pipeline.context_builder import _reciprocal_rank_fusion

        vec_hits = [(1, 0.1), (2, 0.2)]
        fts_hits = [(2, 5.0), (1, 1.0)]

        fused = _reciprocal_rank_fusion(vec_hits, fts_hits, vector_weight=1.0)
        ids = [doc_id for doc_id, _ in fused]
        # With weight=1.0, vector ranking dominates: doc 1 first
        assert ids[0] == 1


# ---------------------------------------------------------------------------
# Distance threshold
# ---------------------------------------------------------------------------
class TestDistanceThreshold:
    @pytest.mark.asyncio
    async def test_max_distance_filters_results(self, mock_ollama: AsyncMock):
        """Context docs beyond max_distance should be excluded."""
        doc_a = _make_doc(10)
        target = _make_doc(42, inbox=True)

        paperless = AsyncMock()
        paperless.get_document = AsyncMock(return_value=doc_a)

        with (
            patch("app.pipeline.context_builder._find_similar_ids") as mock_knn,
            patch("app.pipeline.context_builder.settings") as mock_settings,
        ):
            mock_settings.context_max_docs = 5
            mock_settings.context_max_distance = 0.3
            mock_settings.embed_max_chars = 1000
            # Return one doc within threshold, one beyond
            mock_knn.return_value = [(10, 0.15)]

            result = await find_similar_with_distances(target, paperless, mock_ollama, limit=5)

        assert len(result) == 1
        # Verify max_distance was passed
        call_kwargs = mock_knn.call_args
        assert call_kwargs.kwargs.get("max_distance") == 0.3


# ---------------------------------------------------------------------------
# Hybrid search integration
# ---------------------------------------------------------------------------
class TestHybridSearch:
    @pytest.mark.asyncio
    async def test_falls_back_to_vector_when_no_fts(self, mock_ollama: AsyncMock):
        """When FTS returns no results, hybrid search uses pure vector."""
        doc_a = _make_doc(10)
        paperless = AsyncMock()
        paperless.get_document = AsyncMock(return_value=doc_a)

        with (
            patch("app.pipeline.context_builder._find_similar_ids") as mock_vec,
            patch("app.pipeline.context_builder._fts_search", return_value=[]),
        ):
            mock_vec.return_value = [(10, 0.15)]

            from app.pipeline.context_builder import find_similar_by_query_text

            result = await find_similar_by_query_text("test query", paperless, mock_ollama, limit=5)

        assert len(result) == 1
        assert result[0].document.id == 10
