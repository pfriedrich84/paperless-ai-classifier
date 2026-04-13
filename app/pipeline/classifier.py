"""Core classification logic: build prompt, call LLM, parse result."""

from __future__ import annotations

import json
from pathlib import Path

import structlog

from app.clients.ollama import OllamaClient
from app.config import settings
from app.models import ClassificationResult, PaperlessDocument, PaperlessEntity

log = structlog.get_logger(__name__)


def _prompt_override_path() -> Path:
    """Path for user-edited prompt override in the persistent data dir."""
    return Path(settings.data_dir) / "classify_system.txt"


def _load_system_prompt() -> str:
    """Load system prompt — user override in /data takes precedence over built-in default."""
    override = _prompt_override_path()
    if override.is_file():
        return override.read_text(encoding="utf-8")
    return (settings.prompts_dir / "classify_system.txt").read_text(encoding="utf-8")


def _truncate(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    return text[:limit] + "\n...[abgeschnitten]"


def _estimate_tokens(text: str) -> int:
    """Rough chars-to-tokens estimate (~3.0 chars/token for multilingual German)."""
    return max(1, len(text) * 10 // 30)


def _tokens_to_chars(tokens: int) -> int:
    """Convert a token budget back to approximate character count."""
    return tokens * 30 // 10


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
    *,
    num_ctx: int = 8192,
    system_prompt_chars: int = 0,
) -> str:
    # --- Fixed sections (entity lists + task instructions) ---
    entity_lines: list[str] = [
        "# Verfuegbare Entitaeten in Paperless",
        _format_entity_list("Korrespondenten", correspondents),
        _format_entity_list("Dokumenttypen", doctypes),
        _format_entity_list("Speicherpfade", storage_paths),
        _format_entity_list("Tags", tags),
    ]
    entity_section = "\n".join(entity_lines)

    task_section = (
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

    # --- Token budget computation ---
    RESPONSE_RESERVE = 512  # tokens reserved for the model's output
    MIN_CONTEXT_DOC_TOKENS = 100

    system_tokens = _estimate_tokens("x" * system_prompt_chars) if system_prompt_chars else 0
    fixed_tokens = _estimate_tokens(entity_section) + _estimate_tokens(task_section) + 50
    # 15% safety margin — chars-to-tokens estimation is inherently approximate
    doc_budget_tokens = int((num_ctx - RESPONSE_RESERVE - system_tokens - fixed_tokens) * 0.85)

    if doc_budget_tokens < 200:
        log.warning("very tight token budget", budget=doc_budget_tokens, num_ctx=num_ctx)
        doc_budget_tokens = 200

    # Split: target gets 60%, context docs share 40% (target gets all if no context)
    active_context = list(context_docs)
    if active_context:
        target_budget_tokens = int(doc_budget_tokens * 0.6)
        context_budget_tokens = doc_budget_tokens - target_budget_tokens

        # Drop least-similar context docs if per-doc budget is too small
        while active_context:
            per_doc = context_budget_tokens // len(active_context)
            if per_doc >= MIN_CONTEXT_DOC_TOKENS:
                break
            active_context.pop()  # drop least-similar (last) doc

        if not active_context:
            # All context dropped — target gets the full budget
            target_budget_tokens = doc_budget_tokens
    else:
        target_budget_tokens = doc_budget_tokens
        context_budget_tokens = 0

    target_chars = min(_tokens_to_chars(target_budget_tokens), settings.max_doc_chars)
    context_chars_per_doc = (
        _tokens_to_chars(context_budget_tokens // len(active_context)) if active_context else 0
    )

    # --- Assemble prompt ---
    sections: list[str] = [entity_section]

    if active_context:
        sections.append(
            f"\n# Kontext: {len(active_context)} aehnliche bereits klassifizierte Dokumente"
        )
        for c in active_context:
            sections.append(
                _format_context_block(
                    c, context_chars_per_doc, correspondents, doctypes, storage_paths, tags
                )
            )

    sections.append("\n# Zu klassifizierendes Dokument")
    sections.append(_format_document_block(target, target_chars))
    sections.append(task_section)

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
        target,
        context_docs,
        correspondents,
        doctypes,
        storage_paths,
        tags,
        num_ctx=settings.ollama_num_ctx,
        system_prompt_chars=len(system),
    )

    log.info(
        "calling ollama",
        doc_id=target.id,
        context_docs=len(context_docs),
        model=ollama.model,
        prompt_chars=len(user),
        estimated_tokens=_estimate_tokens(system) + _estimate_tokens(user),
    )

    raw = await ollama.chat_json(system=system, user=user)
    raw_str = json.dumps(raw, ensure_ascii=False)

    try:
        result = ClassificationResult.model_validate(raw)
    except Exception as exc:
        log.error("failed to validate classification", error=str(exc), raw=raw_str[:500])
        raise

    return result, raw_str
