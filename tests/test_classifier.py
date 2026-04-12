"""Tests for the classifier prompt builder and entity resolution."""

from __future__ import annotations

from app.models import PaperlessDocument, PaperlessEntity
from app.pipeline.classifier import (
    _estimate_tokens,
    _format_context_block,
    _format_document_block,
    _resolve_entity_name,
    build_user_prompt,
)


# ---------------------------------------------------------------------------
# _resolve_entity_name
# ---------------------------------------------------------------------------
class TestResolveEntityName:
    def test_found(self, sample_correspondents: list[PaperlessEntity]):
        assert _resolve_entity_name(2, sample_correspondents) == "Stadtwerke München"

    def test_not_found(self, sample_correspondents: list[PaperlessEntity]):
        assert _resolve_entity_name(999, sample_correspondents) is None

    def test_none_id(self, sample_correspondents: list[PaperlessEntity]):
        assert _resolve_entity_name(None, sample_correspondents) is None

    def test_empty_list(self):
        assert _resolve_entity_name(1, []) is None


# ---------------------------------------------------------------------------
# _format_context_block
# ---------------------------------------------------------------------------
class TestFormatContextBlock:
    def test_full_metadata(
        self,
        sample_context_doc: PaperlessDocument,
        sample_correspondents: list[PaperlessEntity],
        sample_doctypes: list[PaperlessEntity],
        sample_storage_paths: list[PaperlessEntity],
        sample_tags: list[PaperlessEntity],
    ):
        result = _format_context_block(
            sample_context_doc,
            4000,
            sample_correspondents,
            sample_doctypes,
            sample_storage_paths,
            sample_tags,
        )
        assert "Titel: Stromrechnung Q1 2024" in result
        assert "Datum: 2024-03-15" in result
        assert "Korrespondent: Stadtwerke München" in result
        assert "Dokumenttyp: Rechnung" in result
        assert "Speicherpfad: Finanzen/Rechnungen" in result
        assert "Tags: Finanzen, Strom" in result
        assert "Inhalt:" in result

    def test_no_metadata(
        self,
        sample_correspondents: list[PaperlessEntity],
        sample_doctypes: list[PaperlessEntity],
        sample_storage_paths: list[PaperlessEntity],
        sample_tags: list[PaperlessEntity],
    ):
        """A doc with no classification should only show title + content."""
        doc = PaperlessDocument(id=99, title="Unclassified", content="Some text")
        result = _format_context_block(
            doc,
            4000,
            sample_correspondents,
            sample_doctypes,
            sample_storage_paths,
            sample_tags,
        )
        assert "Titel: Unclassified" in result
        assert "Korrespondent:" not in result
        assert "Dokumenttyp:" not in result
        assert "Speicherpfad:" not in result
        assert "Tags:" not in result
        assert "Datum:" not in result

    def test_partial_metadata(
        self,
        sample_correspondents: list[PaperlessEntity],
        sample_doctypes: list[PaperlessEntity],
        sample_storage_paths: list[PaperlessEntity],
        sample_tags: list[PaperlessEntity],
    ):
        """Only populated metadata lines should appear."""
        doc = PaperlessDocument(
            id=7,
            title="Partial",
            content="text",
            correspondent=2,
            document_type=None,
            tags=[],
        )
        result = _format_context_block(
            doc,
            4000,
            sample_correspondents,
            sample_doctypes,
            sample_storage_paths,
            sample_tags,
        )
        assert "Korrespondent: Stadtwerke München" in result
        assert "Dokumenttyp:" not in result
        assert "Tags:" not in result

    def test_unresolvable_tags_skipped(
        self,
        sample_correspondents: list[PaperlessEntity],
        sample_doctypes: list[PaperlessEntity],
        sample_storage_paths: list[PaperlessEntity],
        sample_tags: list[PaperlessEntity],
    ):
        """Tags with IDs not in the entity list should be silently skipped."""
        doc = PaperlessDocument(id=8, title="Test", content="x", tags=[20, 999])
        result = _format_context_block(
            doc,
            4000,
            sample_correspondents,
            sample_doctypes,
            sample_storage_paths,
            sample_tags,
        )
        assert "Tags: Finanzen" in result
        assert "999" not in result


# ---------------------------------------------------------------------------
# _format_document_block (regression — target doc must stay simple)
# ---------------------------------------------------------------------------
class TestFormatDocumentBlock:
    def test_target_has_no_metadata(self, sample_doc: PaperlessDocument):
        result = _format_document_block(sample_doc, 8000)
        assert "Titel:" in result
        assert "Inhalt:" in result
        assert "Korrespondent:" not in result
        assert "Dokumenttyp:" not in result
        assert "Tags:" not in result


# ---------------------------------------------------------------------------
# build_user_prompt
# ---------------------------------------------------------------------------
class TestBuildUserPrompt:
    def test_context_docs_include_metadata(
        self,
        sample_doc: PaperlessDocument,
        sample_context_doc: PaperlessDocument,
        sample_correspondents: list[PaperlessEntity],
        sample_doctypes: list[PaperlessEntity],
        sample_storage_paths: list[PaperlessEntity],
        sample_tags: list[PaperlessEntity],
    ):
        prompt = build_user_prompt(
            target=sample_doc,
            context_docs=[sample_context_doc],
            correspondents=sample_correspondents,
            doctypes=sample_doctypes,
            storage_paths=sample_storage_paths,
            tags=sample_tags,
        )
        # Context section has metadata
        assert "1 aehnliche bereits klassifizierte Dokumente" in prompt
        assert "Korrespondent: Stadtwerke München" in prompt
        assert "Dokumenttyp: Rechnung" in prompt
        assert "Speicherpfad: Finanzen/Rechnungen" in prompt
        assert "Tags: Finanzen, Strom" in prompt

        # Target section comes after and has NO metadata
        target_section = prompt.split("# Zu klassifizierendes Dokument")[1]
        assert "Korrespondent:" not in target_section
        assert "Dokumenttyp:" not in target_section

    def test_no_context_docs(
        self,
        sample_doc: PaperlessDocument,
        sample_correspondents: list[PaperlessEntity],
        sample_doctypes: list[PaperlessEntity],
        sample_storage_paths: list[PaperlessEntity],
        sample_tags: list[PaperlessEntity],
    ):
        prompt = build_user_prompt(
            target=sample_doc,
            context_docs=[],
            correspondents=sample_correspondents,
            doctypes=sample_doctypes,
            storage_paths=sample_storage_paths,
            tags=sample_tags,
        )
        assert "aehnliche bereits klassifizierte Dokumente" not in prompt
        assert "# Zu klassifizierendes Dokument" in prompt


# ---------------------------------------------------------------------------
# Token budget — build_user_prompt respects num_ctx
# ---------------------------------------------------------------------------
class TestPromptBudget:
    def _make_long_doc(self, doc_id: int = 1, content_len: int = 20000) -> PaperlessDocument:
        return PaperlessDocument(
            id=doc_id,
            title="Long Doc",
            content="x" * content_len,
        )

    def _make_context_docs(self, count: int, content_len: int = 5000) -> list[PaperlessDocument]:
        return [
            PaperlessDocument(
                id=100 + i,
                title=f"Context {i}",
                content="y" * content_len,
                correspondent=1,
                document_type=10,
                tags=[20],
            )
            for i in range(count)
        ]

    def test_respects_token_budget(
        self,
        sample_correspondents: list[PaperlessEntity],
        sample_doctypes: list[PaperlessEntity],
        sample_storage_paths: list[PaperlessEntity],
        sample_tags: list[PaperlessEntity],
    ):
        """With num_ctx=4096 and a large doc, prompt stays within budget."""
        target = self._make_long_doc(content_len=20000)
        context = self._make_context_docs(5, content_len=5000)
        system_chars = 3500  # approximate system prompt size

        prompt = build_user_prompt(
            target=target,
            context_docs=context,
            correspondents=sample_correspondents,
            doctypes=sample_doctypes,
            storage_paths=sample_storage_paths,
            tags=sample_tags,
            num_ctx=4096,
            system_prompt_chars=system_chars,
        )
        total_tokens = _estimate_tokens(prompt) + _estimate_tokens("x" * system_chars)
        # Total should fit within num_ctx (with some margin for response)
        assert total_tokens < 4096

    def test_drops_context_when_tight(
        self,
        sample_correspondents: list[PaperlessEntity],
        sample_doctypes: list[PaperlessEntity],
        sample_storage_paths: list[PaperlessEntity],
        sample_tags: list[PaperlessEntity],
    ):
        """With a very small num_ctx, context docs get dropped."""
        target = self._make_long_doc(content_len=5000)
        context = self._make_context_docs(5, content_len=5000)

        prompt = build_user_prompt(
            target=target,
            context_docs=context,
            correspondents=sample_correspondents,
            doctypes=sample_doctypes,
            storage_paths=sample_storage_paths,
            tags=sample_tags,
            num_ctx=2048,
            system_prompt_chars=3500,
        )
        # With such a tight budget, not all 5 context docs can fit
        context_count = prompt.count("--- Dokument #10")
        assert context_count < 5

    def test_no_context_target_gets_full_budget(
        self,
        sample_correspondents: list[PaperlessEntity],
        sample_doctypes: list[PaperlessEntity],
        sample_storage_paths: list[PaperlessEntity],
        sample_tags: list[PaperlessEntity],
    ):
        """Without context docs, target document gets the full document budget."""
        target = self._make_long_doc(content_len=20000)

        prompt_no_ctx = build_user_prompt(
            target=target,
            context_docs=[],
            correspondents=sample_correspondents,
            doctypes=sample_doctypes,
            storage_paths=sample_storage_paths,
            tags=sample_tags,
            num_ctx=4096,
            system_prompt_chars=3500,
        )
        prompt_with_ctx = build_user_prompt(
            target=target,
            context_docs=self._make_context_docs(1, content_len=100),
            correspondents=sample_correspondents,
            doctypes=sample_doctypes,
            storage_paths=sample_storage_paths,
            tags=sample_tags,
            num_ctx=4096,
            system_prompt_chars=3500,
        )
        # Target section should be larger without context (gets 100% vs 60% of budget)
        target_no_ctx = prompt_no_ctx.split("# Zu klassifizierendes Dokument")[1]
        target_with_ctx = prompt_with_ctx.split("# Zu klassifizierendes Dokument")[1]
        assert len(target_no_ctx) > len(target_with_ctx)
