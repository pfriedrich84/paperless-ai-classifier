"""Multi-level OCR correction: text-only, vision-light, and vision-full.

Corrected text is **never** written back to Paperless — it is stored in our
local ``doc_ocr_cache`` table and used for classification + embedding context.
"""

from __future__ import annotations

import asyncio
import re

import structlog

from app.clients.ollama import OllamaClient
from app.clients.paperless import PaperlessClient
from app.config import settings
from app.db import get_conn
from app.models import PaperlessDocument

log = structlog.get_logger(__name__)

# Characters considered "normal" in German text (letters, digits, common punctuation)
_NORMAL_RE = re.compile(r"[a-zA-ZäöüÄÖÜß0-9\s.,;:!?\-/()\[\]{}\"'@#€$%&+=\n\r\t]")

_VALID_OCR_MODES = {"off", "text", "vision_light", "vision_full"}


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------
def effective_ocr_mode() -> str:
    """Return the active OCR mode, honouring the deprecated ``enable_ocr_correction``."""
    mode = settings.ocr_mode
    if mode != "off":
        return mode if mode in _VALID_OCR_MODES else "off"
    # Backwards compat: enable_ocr_correction=true → "text"
    if settings.enable_ocr_correction:
        log.warning("enable_ocr_correction is deprecated — use OCR_MODE=text instead")
        return "text"
    return "off"


async def maybe_correct_ocr(
    doc: PaperlessDocument,
    ollama: OllamaClient,
    paperless: PaperlessClient | None = None,
) -> tuple[str, int]:
    """Optionally correct OCR errors in *doc.content*.

    Returns ``(text, num_corrections)``.  The corrected text is **not** written
    back to Paperless — it is only used as improved input for classification.
    """
    text = doc.content or ""
    mode = effective_ocr_mode()

    if mode == "off":
        return text, 0
    if mode == "text":
        return await _correct_text_only(doc, ollama)
    if mode == "vision_light":
        return await _correct_vision_light(doc, ollama, paperless)
    if mode == "vision_full":
        return await _correct_vision_full(doc, ollama, paperless)
    return text, 0


def cache_ocr_correction(
    document_id: int,
    corrected_text: str,
    ocr_mode: str,
    num_corrections: int,
) -> None:
    """Store corrected text in ``doc_ocr_cache``."""
    with get_conn() as conn:
        conn.execute(
            """INSERT OR REPLACE INTO doc_ocr_cache
               (document_id, corrected_content, ocr_mode, num_corrections, corrected_at)
               VALUES (?, ?, ?, ?, datetime('now'))""",
            (document_id, corrected_text, ocr_mode, num_corrections),
        )


def get_cached_ocr(document_id: int) -> str | None:
    """Return cached corrected text for a document, or ``None``."""
    with get_conn() as conn:
        row = conn.execute(
            "SELECT corrected_content FROM doc_ocr_cache WHERE document_id = ?",
            (document_id,),
        ).fetchone()
    return row["corrected_content"] if row else None


async def batch_correct_documents(
    paperless: PaperlessClient,
    ollama: OllamaClient,
    *,
    limit: int | None = None,
    force: bool = False,
) -> int:
    """Run OCR correction over indexed documents.

    Skips documents already in ``doc_ocr_cache`` unless *force* is ``True``.
    Returns the number of documents corrected.
    """
    mode = effective_ocr_mode()
    if mode == "off":
        log.info("batch OCR skipped — ocr_mode is off")
        return 0

    # Get all indexed document IDs
    with get_conn() as conn:
        rows = conn.execute("SELECT document_id FROM doc_embedding_meta").fetchall()
        indexed_ids = [row["document_id"] for row in rows]

        if not force:
            cached = conn.execute("SELECT document_id FROM doc_ocr_cache").fetchall()
            cached_ids = {row["document_id"] for row in cached}
            indexed_ids = [did for did in indexed_ids if did not in cached_ids]

    if limit:
        indexed_ids = indexed_ids[:limit]

    log.info("batch OCR starting", total=len(indexed_ids), mode=mode, force=force)
    corrected = 0

    for doc_id in indexed_ids:
        try:
            doc = await paperless.get_document(doc_id)
            text, num = await maybe_correct_ocr(doc, ollama, paperless)
            if num > 0 or mode.startswith("vision"):
                cache_ocr_correction(doc_id, text, mode, num)
                corrected += 1
                log.info("batch OCR corrected", doc_id=doc_id, num_corrections=num)
        except Exception as exc:
            log.warning("batch OCR failed for document", doc_id=doc_id, error=str(exc))

    log.info("batch OCR complete", corrected=corrected, total=len(indexed_ids))
    return corrected


# ---------------------------------------------------------------------------
# Heuristic
# ---------------------------------------------------------------------------
def _text_looks_broken(text: str) -> bool:
    """Heuristic: return True if the text shows typical OCR artefacts."""
    if not text or len(text) < 50:
        return False

    total = len(text)

    # Many '?' can indicate unrecognised glyphs
    q_ratio = text.count("?") / total
    if q_ratio > 0.02:
        return True

    # Many single-character "words" suggest broken tokenisation
    words = text.split()
    if words:
        single_char = sum(
            1 for w in words if len(w) == 1 and w not in {"\u2013", "-", "\u2014", "&"}
        )
        if single_char / len(words) > 0.15:
            return True

    # High ratio of non-standard characters
    non_normal = len(_NORMAL_RE.sub("", text))
    return non_normal / total > 0.03


# ---------------------------------------------------------------------------
# Mode: text (text-only LLM correction)
# ---------------------------------------------------------------------------
async def _correct_text_only(
    doc: PaperlessDocument,
    ollama: OllamaClient,
) -> tuple[str, int]:
    """Text-only OCR correction using a smaller LLM."""
    text = doc.content or ""

    if not _text_looks_broken(text):
        return text, 0

    log.info("ocr text correction triggered", doc_id=doc.id)

    try:
        prompt_path = settings.prompts_dir / "ocr_correction_system.txt"
        system = prompt_path.read_text(encoding="utf-8")
        user_text = text[: settings.max_doc_chars]
        raw = await ollama.chat_json(
            system=system,
            user=user_text,
            model=ollama.ocr_model,
            num_ctx=settings.ollama_ocr_num_ctx,
        )

        corrected = raw.get("corrected_text", text)
        num = int(raw.get("num_corrections", 0))
        log.info("ocr text corrections applied", doc_id=doc.id, num_corrections=num)
        return corrected, num
    except Exception as exc:
        log.warning("ocr text correction failed", doc_id=doc.id, error=str(exc))
        return text, 0


# ---------------------------------------------------------------------------
# Mode: vision_light (image-assisted correction, heuristic-gated)
# ---------------------------------------------------------------------------
async def _correct_vision_light(
    doc: PaperlessDocument,
    ollama: OllamaClient,
    paperless: PaperlessClient | None,
) -> tuple[str, int]:
    """Vision-assisted OCR correction — heuristic-gated, up to N pages."""
    text = doc.content or ""

    if not _text_looks_broken(text):
        return text, 0

    if paperless is None:
        log.warning("vision_light requires paperless client, falling back to text mode")
        return await _correct_text_only(doc, ollama)

    log.info("ocr vision_light triggered", doc_id=doc.id)

    try:
        file_bytes, content_type = await paperless.download_document(doc.id)
        images = await asyncio.to_thread(
            _render_pages, file_bytes, content_type, settings.ocr_vision_max_pages
        )
        if not images:
            log.warning("no pages rendered, falling back to text mode", doc_id=doc.id)
            return await _correct_text_only(doc, ollama)

        prompt_path = settings.prompts_dir / "ocr_vision_light_system.txt"
        system = prompt_path.read_text(encoding="utf-8")
        user_text = text[: settings.max_doc_chars]
        vision_model = settings.ocr_vision_model or ollama.model

        raw = await ollama.chat_vision_json(
            system=system,
            user=user_text,
            images=images,
            model=vision_model,
            num_ctx=settings.ollama_ocr_num_ctx,
        )

        corrected = raw.get("corrected_text", text)
        num = int(raw.get("num_corrections", 0))
        log.info("ocr vision_light corrections applied", doc_id=doc.id, num_corrections=num)
        return corrected, num
    except Exception as exc:
        log.warning(
            "ocr vision_light failed, falling back to text mode", doc_id=doc.id, error=str(exc)
        )
        return await _correct_text_only(doc, ollama)


# ---------------------------------------------------------------------------
# Mode: vision_full (per-page correction, always runs)
# ---------------------------------------------------------------------------
async def _correct_vision_full(
    doc: PaperlessDocument,
    ollama: OllamaClient,
    paperless: PaperlessClient | None,
) -> tuple[str, int]:
    """Full vision OCR — per-page correction, always runs (no heuristic gate)."""
    text = doc.content or ""

    if paperless is None:
        log.warning("vision_full requires paperless client, falling back to text mode")
        return await _correct_text_only(doc, ollama)

    log.info("ocr vision_full triggered", doc_id=doc.id)

    try:
        file_bytes, content_type = await paperless.download_document(doc.id)
        images = await asyncio.to_thread(
            _render_pages, file_bytes, content_type, settings.ocr_vision_max_pages
        )
        if not images:
            log.warning("no pages rendered, falling back to text mode", doc_id=doc.id)
            return await _correct_text_only(doc, ollama)

        # Split OCR text into per-page chunks
        page_texts = _split_text_by_pages(text, len(images))

        prompt_path = settings.prompts_dir / "ocr_vision_full_system.txt"
        system = prompt_path.read_text(encoding="utf-8")
        vision_model = settings.ocr_vision_model or ollama.model

        corrected_pages: list[str] = []
        total_corrections = 0

        for i, (page_image, page_text) in enumerate(zip(images, page_texts, strict=False)):
            try:
                raw = await ollama.chat_vision_json(
                    system=system,
                    user=page_text or "(Diese Seite hat keinen OCR-Text.)",
                    images=[page_image],
                    model=vision_model,
                    num_ctx=settings.ollama_ocr_num_ctx,
                )
                corrected = raw.get("corrected_text", page_text)
                num = int(raw.get("num_corrections", 0))
                corrected_pages.append(corrected)
                total_corrections += num
                log.debug("vision_full page done", doc_id=doc.id, page=i + 1, corrections=num)
            except Exception as exc:
                log.warning(
                    "vision_full page failed, keeping original",
                    doc_id=doc.id,
                    page=i + 1,
                    error=str(exc),
                )
                corrected_pages.append(page_text)

        merged = "\n\n".join(corrected_pages)
        log.info(
            "ocr vision_full complete",
            doc_id=doc.id,
            pages=len(images),
            total_corrections=total_corrections,
        )
        return merged, total_corrections
    except Exception as exc:
        log.warning(
            "ocr vision_full failed, falling back to vision_light", doc_id=doc.id, error=str(exc)
        )
        return await _correct_vision_light(doc, ollama, paperless)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def _render_pages(file_bytes: bytes, content_type: str, max_pages: int) -> list[str]:
    """Render document pages — called in a thread to avoid blocking the event loop."""
    from app.pipeline.pdf_renderer import render_document_pages

    return render_document_pages(
        file_bytes,
        content_type,
        max_pages=max_pages,
        dpi=settings.ocr_vision_dpi,
    )


def _split_text_by_pages(text: str, num_pages: int) -> list[str]:
    """Split OCR text into per-page chunks.

    Paperless-NGX uses form feed (``\\f``) as page separator.
    If no form feeds are present, divide the text evenly.
    """
    if "\f" in text:
        parts = text.split("\f")
        # Pad or trim to match page count
        while len(parts) < num_pages:
            parts.append("")
        return parts[:num_pages]

    # No form feeds — divide evenly by character count
    if num_pages <= 1:
        return [text]
    chunk_size = max(1, len(text) // num_pages)
    parts = []
    for i in range(num_pages):
        start = i * chunk_size
        end = start + chunk_size if i < num_pages - 1 else len(text)
        parts.append(text[start:end])
    return parts
