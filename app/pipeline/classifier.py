"""Core classification logic: build prompt, call LLM, parse result."""

from __future__ import annotations

import json

import structlog

from app.clients.ollama import OllamaClient
from app.config import settings
from app.models import ClassificationResult, PaperlessDocument, PaperlessEntity

log = structlog.get_logger(__name__)


def _load_system_prompt() -> str:
    path = settings.prompts_dir / "classify_system.txt"
    return path.read_text(encoding="utf-8")


def _truncate(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    return text[:limit] + "\n...[abgeschnitten]"


def _format_document_block(doc: PaperlessDocument, max_chars: int) -> str:
    return (
        f"--- Dokument #{doc.id} ---\n"
        f"Titel: {doc.title}\n"
        f"Inhalt:\n{_truncate(doc.content or '', max_chars)}\n"
    )


def _resolve_entity_name(entity_id: int | None, entities: list[PaperlessEntity]) -> str | None:
    """Resolve a Paperless entity ID to its display name (inverse of name→ID)."""
    if entity_id is None:
        return None
    for e in entities:
        if e.id == entity_id:
            return e.name
    return None


def _format_context_block(
    doc: PaperlessDocument,
    max_chars: int,
    correspondents: list[PaperlessEntity],
    doctypes: list[PaperlessEntity],
    storage_paths: list[PaperlessEntity],
    tags: list[PaperlessEntity],
) -> str:
    """Format a context document including its classification metadata."""
    lines = [f"--- Dokument #{doc.id} ---", f"Titel: {doc.title}"]

    if doc.created_date:
        lines.append(f"Datum: {doc.created_date}")

    corr = _resolve_entity_name(doc.correspondent, correspondents)
    if corr:
        lines.append(f"Korrespondent: {corr}")

    dt = _resolve_entity_name(doc.document_type, doctypes)
    if dt:
        lines.append(f"Dokumenttyp: {dt}")

    sp = _resolve_entity_name(doc.storage_path, storage_paths)
    if sp:
        lines.append(f"Speicherpfad: {sp}")

    if doc.tags:
        tag_names = [name for tid in doc.tags if (name := _resolve_entity_name(tid, tags))]
        if tag_names:
            lines.append(f"Tags: {', '.join(tag_names)}")

    lines.append(f"Inhalt:\n{_truncate(doc.content or '', max_chars)}")
    return "\n".join(lines) + "\n"


def _format_entity_list(label: str, entities: list[PaperlessEntity]) -> str:
    if not entities:
        return f"{label}: (keine)"
    names = ", ".join(e.name for e in entities[:100])
    return f"{label}: {names}"


def build_user_prompt(
    target: PaperlessDocument,
    context_docs: list[PaperlessDocument],
    correspondents: list[PaperlessEntity],
    doctypes: list[PaperlessEntity],
    storage_paths: list[PaperlessEntity],
    tags: list[PaperlessEntity],
) -> str:
    max_chars = settings.max_doc_chars

    sections: list[str] = []

    sections.append("# Verfuegbare Entitaeten in Paperless")
    sections.append(_format_entity_list("Korrespondenten", correspondents))
    sections.append(_format_entity_list("Dokumenttypen", doctypes))
    sections.append(_format_entity_list("Speicherpfade", storage_paths))
    sections.append(_format_entity_list("Tags", tags))

    if context_docs:
        sections.append(
            f"\n# Kontext: {len(context_docs)} aehnliche bereits klassifizierte Dokumente"
        )
        for c in context_docs:
            sections.append(
                _format_context_block(
                    c, max_chars // 2, correspondents, doctypes, storage_paths, tags
                )
            )

    sections.append("\n# Zu klassifizierendes Dokument")
    sections.append(_format_document_block(target, max_chars))

    sections.append(
        "\n# Aufgabe\n"
        "Gib ein JSON-Objekt mit folgenden Feldern zurueck:\n"
        "- title (string)\n"
        "- date (string, YYYY-MM-DD oder null)\n"
        "- correspondent (string, Name)\n"
        "- document_type (string, Name)\n"
        "- storage_path (string, Name oder null)\n"
        "- tags (liste von objekten {name, confidence})\n"
        "- confidence (0-100, Gesamtvertrauen)\n"
        "- reasoning (kurze Begruendung in 1-3 Saetzen)\n"
    )

    return "\n".join(sections)


async def classify(
    target: PaperlessDocument,
    context_docs: list[PaperlessDocument],
    correspondents: list[PaperlessEntity],
    doctypes: list[PaperlessEntity],
    storage_paths: list[PaperlessEntity],
    tags: list[PaperlessEntity],
    ollama: OllamaClient,
) -> tuple[ClassificationResult, str]:
    """Call the LLM and return (parsed result, raw JSON string)."""
    system = _load_system_prompt()
    user = build_user_prompt(target, context_docs, correspondents, doctypes, storage_paths, tags)

    log.info(
        "calling ollama",
        doc_id=target.id,
        context_docs=len(context_docs),
        model=ollama.model,
    )

    raw = await ollama.chat_json(system=system, user=user)
    raw_str = json.dumps(raw, ensure_ascii=False)

    try:
        result = ClassificationResult.model_validate(raw)
    except Exception as exc:
        log.error("failed to validate classification", error=str(exc), raw=raw_str[:500])
        raise

    return result, raw_str
