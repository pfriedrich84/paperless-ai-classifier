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
        sections.append("\n# Kontext: aehnliche bereits klassifizierte Dokumente")
        for c in context_docs:
            sections.append(_format_document_block(c, max_chars // 2))

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
    user = build_user_prompt(
        target, context_docs, correspondents, doctypes, storage_paths, tags
    )

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
