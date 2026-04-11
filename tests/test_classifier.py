"""Tests for the classifier prompt builder and entity resolution."""

from __future__ import annotations

from app.models import PaperlessDocument, PaperlessEntity
from app.pipeline.classifier import (
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
