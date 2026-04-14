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


# ---------------------------------------------------------------------------
# Inbox filter tests
# ---------------------------------------------------------------------------
class TestInboxFilter:
    @pytest.mark.asyncio
    async def test_inbox_filter_in_search_expression(
        self, mock_ollama: AsyncMock, mock_meili: AsyncMock
    ):
        """Meilisearch filter expression should exclude inbox-tagged documents."""
        classified_doc = _make_doc(10, correspondent=2, document_type=10)
        target = _make_doc(42, inbox=True)

        paperless = AsyncMock()
        paperless.get_document = AsyncMock(return_value=classified_doc)

        from app.clients.meilisearch import MeiliHit

        # Meilisearch returns only non-inbox docs (filter applied server-side)
        mock_meili.vector_search = AsyncMock(return_value=[MeiliHit(doc_id=10, score=0.9)])
        await find_similar_documents(target, paperless, mock_ollama, mock_meili, limit=5)

        # Verify filter expression contains inbox tag ID
        call_kwargs = mock_meili.vector_search.call_args
        filter_expr = call_kwargs.kwargs.get("filter_expr", "")
        assert str(INBOX_TAG_ID) in filter_expr
        assert "NOT IN" in filter_expr

    @pytest.mark.asyncio
    async def test_non_inbox_docs_included(self, mock_ollama: AsyncMock, mock_meili: AsyncMock):
        """Documents without the inbox tag should be included as context."""
        doc_a = _make_doc(10, extra_tags=[20])
        doc_b = _make_doc(11, extra_tags=[21])
        target = _make_doc(42, inbox=True)

        paperless = AsyncMock()
        paperless.get_document = AsyncMock(
            side_effect=lambda doc_id: {10: doc_a, 11: doc_b}[doc_id]
        )

        from app.clients.meilisearch import MeiliHit

        mock_meili.vector_search = AsyncMock(
            return_value=[MeiliHit(doc_id=10, score=0.9), MeiliHit(doc_id=11, score=0.8)]
        )
        result = await find_similar_documents(target, paperless, mock_ollama, mock_meili, limit=5)

        assert len(result) == 2
        assert [d.id for d in result] == [10, 11]

    @pytest.mark.asyncio
    async def test_all_inbox_returns_empty(self, mock_ollama: AsyncMock, mock_meili: AsyncMock):
        """If all candidates are in the inbox, return empty list."""
        target = _make_doc(42, inbox=True)

        paperless = AsyncMock()

        # Meilisearch filter excludes inbox docs, so vector_search returns empty
        mock_meili.vector_search = AsyncMock(return_value=[])
        result = await find_similar_documents(target, paperless, mock_ollama, mock_meili, limit=5)

        assert result == []


# ---------------------------------------------------------------------------
# Overfetch compensation
# ---------------------------------------------------------------------------
class TestOverfetchCompensation:
    @pytest.mark.asyncio
    async def test_limit_respected_despite_filtering(
        self, mock_ollama: AsyncMock, mock_meili: AsyncMock
    ):
        """Even when some candidates are filtered, the result should not exceed limit."""
        docs = {
            1: _make_doc(1),
            3: _make_doc(3),
            5: _make_doc(5),
        }
        target = _make_doc(42, inbox=True)

        paperless = AsyncMock()
        paperless.get_document = AsyncMock(side_effect=lambda doc_id: docs[doc_id])

        from app.clients.meilisearch import MeiliHit

        # Meilisearch returns only non-inbox docs (filter applied server-side)
        mock_meili.vector_search = AsyncMock(
            return_value=[
                MeiliHit(doc_id=1, score=0.9),
                MeiliHit(doc_id=3, score=0.8),
                MeiliHit(doc_id=5, score=0.7),
            ]
        )
        result = await find_similar_documents(target, paperless, mock_ollama, mock_meili, limit=3)

        assert len(result) == 3
        assert all(INBOX_TAG_ID not in d.tags for d in result)

    @pytest.mark.asyncio
    async def test_fewer_than_limit_when_not_enough_candidates(
        self, mock_ollama: AsyncMock, mock_meili: AsyncMock
    ):
        """If there aren't enough non-inbox candidates, return what we have."""
        docs = {
            1: _make_doc(1),
        }
        target = _make_doc(42, inbox=True)

        paperless = AsyncMock()
        paperless.get_document = AsyncMock(side_effect=lambda doc_id: docs[doc_id])

        from app.clients.meilisearch import MeiliHit

        mock_meili.vector_search = AsyncMock(return_value=[MeiliHit(doc_id=1, score=0.9)])
        result = await find_similar_documents(target, paperless, mock_ollama, mock_meili, limit=5)

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
    async def test_empty_content_doc_skipped_by_find_similar(
        self, mock_ollama: AsyncMock, mock_meili: AsyncMock
    ):
        """A target doc with empty content should return no context."""
        target = PaperlessDocument(id=1, title="", content="")
        paperless = AsyncMock()
        result = await find_similar_documents(target, paperless, mock_ollama, mock_meili, limit=5)
        assert result == []
        mock_ollama.embed.assert_not_called()


# ---------------------------------------------------------------------------
# find_similar_with_distances
# ---------------------------------------------------------------------------
class TestFindSimilarWithDistances:
    @pytest.mark.asyncio
    async def test_returns_distance_scores(self, mock_ollama: AsyncMock, mock_meili: AsyncMock):
        """Results should include distance scores from the vector search."""
        doc_a = _make_doc(10)
        doc_b = _make_doc(20)
        target = _make_doc(42, inbox=True)

        paperless = AsyncMock()
        paperless.get_document = AsyncMock(
            side_effect=lambda doc_id: {10: doc_a, 20: doc_b}[doc_id]
        )

        from app.clients.meilisearch import MeiliHit

        mock_meili.vector_search = AsyncMock(
            return_value=[MeiliHit(doc_id=10, score=0.85), MeiliHit(doc_id=20, score=0.58)]
        )
        results = await find_similar_with_distances(
            target, paperless, mock_ollama, mock_meili, limit=5
        )

        assert len(results) == 2
        assert results[0].document.id == 10
        assert results[1].document.id == 20

    @pytest.mark.asyncio
    async def test_inbox_filter_expression_with_distances(
        self, mock_ollama: AsyncMock, mock_meili: AsyncMock
    ):
        """Meilisearch filter should exclude inbox docs; only classified docs returned."""
        classified = _make_doc(10)
        target = _make_doc(42, inbox=True)

        paperless = AsyncMock()
        paperless.get_document = AsyncMock(return_value=classified)

        from app.clients.meilisearch import MeiliHit

        mock_meili.vector_search = AsyncMock(return_value=[MeiliHit(doc_id=10, score=0.8)])
        results = await find_similar_with_distances(
            target, paperless, mock_ollama, mock_meili, limit=5
        )

        assert len(results) == 1
        assert results[0].document.id == 10
        call_kwargs = mock_meili.vector_search.call_args
        filter_expr = call_kwargs.kwargs.get("filter_expr", "")
        assert str(INBOX_TAG_ID) in filter_expr

    @pytest.mark.asyncio
    async def test_delegates_to_find_similar_documents(
        self, mock_ollama: AsyncMock, mock_meili: AsyncMock
    ):
        """find_similar_documents should return the same docs (without distances)."""
        doc_a = _make_doc(10)
        target = _make_doc(42, inbox=True)

        paperless = AsyncMock()
        paperless.get_document = AsyncMock(return_value=doc_a)

        from app.clients.meilisearch import MeiliHit

        mock_meili.vector_search = AsyncMock(return_value=[MeiliHit(doc_id=10, score=0.75)])
        result = await find_similar_documents(target, paperless, mock_ollama, mock_meili, limit=5)

        assert len(result) == 1
        assert result[0].id == 10
        # Result is PaperlessDocument, not SimilarDocument
        assert not hasattr(result[0], "distance")


# ---------------------------------------------------------------------------
# find_similar_by_id
# ---------------------------------------------------------------------------
class TestFindSimilarById:
    @pytest.mark.asyncio
    async def test_returns_empty_for_unknown_doc(self, mock_meili: AsyncMock):
        """If the document has no embedding, return empty list."""
        mock_meili.get_document_vector = AsyncMock(return_value=None)

        result = await find_similar_by_id(999, mock_meili, limit=5)

        assert result == []

    @pytest.mark.asyncio
    async def test_returns_id_distance_pairs(self, mock_meili: AsyncMock):
        """Should return (doc_id, distance) tuples excluding source doc."""
        from app.clients.meilisearch import MeiliHit

        mock_meili.get_document_vector = AsyncMock(return_value=[0.1] * 768)
        mock_meili.vector_search = AsyncMock(
            return_value=[
                MeiliHit(doc_id=10, score=0.85),
                MeiliHit(doc_id=20, score=0.68),
            ]
        )

        result = await find_similar_by_id(42, mock_meili, limit=5)

        assert len(result) == 2
        assert result[0][0] == 10
        assert result[1][0] == 20

    @pytest.mark.asyncio
    async def test_respects_limit(self, mock_meili: AsyncMock):
        """Should not return more than `limit` results."""
        from app.clients.meilisearch import MeiliHit

        mock_meili.get_document_vector = AsyncMock(return_value=[0.1] * 768)
        mock_meili.vector_search = AsyncMock(
            return_value=[
                MeiliHit(doc_id=10, score=0.9),
                MeiliHit(doc_id=20, score=0.8),
            ]
        )

        result = await find_similar_by_id(42, mock_meili, limit=2)

        assert len(result) == 2


# ---------------------------------------------------------------------------
# find_similar_with_precomputed_embedding
# ---------------------------------------------------------------------------
class TestFindSimilarWithPrecomputedEmbedding:
    @pytest.mark.asyncio
    async def test_uses_precomputed_embedding(self, mock_meili: AsyncMock):
        """Should use the provided embedding without calling ollama.embed()."""
        doc_a = _make_doc(10)
        target = _make_doc(42, inbox=True)
        embedding = [0.1] * 768

        paperless = AsyncMock()
        paperless.get_document = AsyncMock(return_value=doc_a)

        from app.clients.meilisearch import MeiliHit

        mock_meili.vector_search = AsyncMock(return_value=[MeiliHit(doc_id=10, score=0.75)])

        results = await find_similar_with_precomputed_embedding(
            target, embedding, paperless, mock_meili, limit=5
        )

        assert len(results) == 1
        assert results[0].document.id == 10
        # distance = 1.0 - score
        assert results[0].distance == pytest.approx(0.25)
        mock_meili.vector_search.assert_called_once()

    @pytest.mark.asyncio
    async def test_excludes_self_and_inbox_via_filter(self, mock_meili: AsyncMock):
        """Filter expression should exclude source doc and inbox-tagged docs."""
        doc_a = _make_doc(10)
        target = _make_doc(42, inbox=True)
        embedding = [0.1] * 768

        paperless = AsyncMock()
        paperless.get_document = AsyncMock(return_value=doc_a)

        from app.clients.meilisearch import MeiliHit

        mock_meili.vector_search = AsyncMock(return_value=[MeiliHit(doc_id=10, score=0.8)])

        await find_similar_with_precomputed_embedding(
            target, embedding, paperless, mock_meili, limit=5
        )

        # Verify the filter expression excludes inbox tag and source doc
        call_kwargs = mock_meili.vector_search.call_args
        filter_expr = call_kwargs.kwargs.get("filter_expr", "")
        assert str(INBOX_TAG_ID) in filter_expr
        assert str(target.id) in filter_expr

    @pytest.mark.asyncio
    async def test_empty_results(self, mock_meili: AsyncMock):
        """Should return empty list when Meilisearch returns no hits."""
        target = _make_doc(42, inbox=True)
        embedding = [0.1] * 768

        paperless = AsyncMock()
        mock_meili.vector_search = AsyncMock(return_value=[])

        results = await find_similar_with_precomputed_embedding(
            target, embedding, paperless, mock_meili, limit=5
        )

        assert results == []


# ---------------------------------------------------------------------------
# store_embedding
# ---------------------------------------------------------------------------
class TestStoreEmbedding:
    @pytest.mark.asyncio
    async def test_writes_to_meili_and_meta(self, mock_meili: AsyncMock):
        """store_embedding should write to Meilisearch and doc_embedding_meta."""
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
            await store_embedding(doc, embedding, mock_meili)

        # Meilisearch upsert called with correct doc_id
        mock_meili.upsert_document.assert_called_once()
        call_kwargs = mock_meili.upsert_document.call_args
        assert call_kwargs.kwargs.get("doc_id") == 42 or call_kwargs[1].get("doc_id") == 42

        # doc_embedding_meta INSERT executed
        assert mock_conn.execute.call_count == 1
        meta_sql = mock_conn.execute.call_args_list[0][0][0]
        assert "doc_embedding_meta" in meta_sql


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
        """Total output should be limited to embed_max_chars (default 1000)."""
        doc = _make_doc(1, content="x" * 2000)
        result = document_summary(doc)
        assert len(result) <= 1000

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
