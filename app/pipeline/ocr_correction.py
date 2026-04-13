"""Optional OCR error correction pass via LLM."""

from __future__ import annotations

import re

import structlog

from app.clients.ollama import OllamaClient
from app.config import settings
from app.models import PaperlessDocument

log = structlog.get_logger(__name__)

# Characters considered "normal" in German text (letters, digits, common punctuation)
_NORMAL_RE = re.compile(r"[a-zA-ZäöüÄÖÜß0-9\s.,;:!?\-/()\[\]{}\"'@#€$%&+=\n\r\t]")


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


async def maybe_correct_ocr(
    doc: PaperlessDocument,
    ollama: OllamaClient,
) -> tuple[str, int]:
    """Optionally correct OCR errors in *doc.content*.

    Returns ``(text, num_corrections)``.  The corrected text is **not** written
    back to Paperless — it is only used as improved input for classification.
    """
    text = doc.content or ""
    if not settings.enable_ocr_correction:
        return text, 0

    if not _text_looks_broken(text):
        return text, 0

    log.info("ocr correction triggered", doc_id=doc.id)

    try:
        prompt_path = settings.prompts_dir / "ocr_correction_system.txt"
        system = prompt_path.read_text(encoding="utf-8")
        user_text = text[: settings.max_doc_chars]
        raw = await ollama.chat_json(system=system, user=user_text, model=ollama.ocr_model)

        corrected = raw.get("corrected_text", text)
        num = int(raw.get("num_corrections", 0))
        log.info("ocr corrections applied", doc_id=doc.id, num_corrections=num)
        return corrected, num
    except Exception as exc:
        log.warning("ocr correction failed", doc_id=doc.id, error=str(exc))
        return text, 0
