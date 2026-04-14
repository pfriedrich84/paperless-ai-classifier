"""CLI management commands for manual pipeline triggering.

Usage::

    python -m app.cli reindex          # Full reindex (OCR + embedding)
    python -m app.cli reindex-ocr      # OCR correction only
    python -m app.cli reindex-embed    # Embedding only (skip OCR)
    python -m app.cli poll             # Process inbox (OCR + embed + classify)
"""

from __future__ import annotations

import asyncio
import logging
import sys

import structlog

from app.clients.ollama import OllamaClient
from app.clients.paperless import PaperlessClient
from app.config import settings
from app.db import init_db


def _configure_logging() -> None:
    """Set up structlog for CLI use (always console renderer)."""
    log_level = getattr(logging, settings.log_level.upper(), logging.INFO)
    structlog.configure(
        processors=[
            structlog.contextvars.merge_contextvars,
            structlog.processors.add_log_level,
            structlog.processors.StackInfoRenderer(),
            structlog.dev.set_exc_info,
            structlog.processors.TimeStamper(fmt="iso"),
            structlog.dev.ConsoleRenderer(),
        ],
        wrapper_class=structlog.make_filtering_bound_logger(log_level),
        context_class=dict,
        logger_factory=structlog.PrintLoggerFactory(),
        cache_logger_on_first_use=True,
    )


async def cmd_reindex() -> None:
    """Full reindex: OCR correction (if enabled) + embedding."""
    from app.indexer import reindex_all

    paperless = PaperlessClient()
    ollama = OllamaClient()
    try:
        count = await reindex_all(paperless, ollama)
        print(f"Reindex complete: {count} documents indexed.")
    finally:
        await paperless.aclose()
        await ollama.aclose()


async def cmd_reindex_ocr() -> None:
    """Run OCR correction on all indexed documents (respects OCR_MODE)."""
    from app.pipeline.ocr_correction import batch_correct_documents, effective_ocr_mode

    mode = effective_ocr_mode()
    if mode == "off":
        print("OCR_MODE is 'off' — nothing to do. Set OCR_MODE to text/vision_light/vision_full.")
        return

    paperless = PaperlessClient()
    ollama = OllamaClient()
    try:
        corrected = await batch_correct_documents(paperless, ollama)
        print(f"OCR correction complete: {corrected} documents corrected (mode={mode}).")
    finally:
        await paperless.aclose()
        await ollama.aclose()


async def cmd_reindex_embed() -> None:
    """Rebuild embeddings only (skip OCR, use cached OCR text if available)."""
    from app.db import get_conn
    from app.indexer import initial_index

    # Clear existing embeddings
    with get_conn() as conn:
        conn.execute("DELETE FROM doc_embedding_meta")
        conn.execute("DELETE FROM doc_embeddings")
    print("Cleared existing embeddings.")

    paperless = PaperlessClient()
    ollama = OllamaClient()
    try:
        count = await initial_index(paperless, ollama)
        print(f"Embedding complete: {count} documents indexed.")
    finally:
        await paperless.aclose()
        await ollama.aclose()


async def cmd_poll() -> None:
    """Process inbox: OCR + embed + classify (same as scheduled poll)."""
    from app.worker import poll_inbox

    paperless = PaperlessClient()
    ollama = OllamaClient()

    # The worker needs module-level client refs — set them via start_scheduler's pattern
    import app.worker as worker

    worker._paperless = paperless
    worker._ollama = ollama

    try:
        await poll_inbox()
        print("Inbox processing complete.")
    finally:
        await paperless.aclose()
        await ollama.aclose()


COMMANDS = {
    "reindex": ("Full reindex (OCR + embedding)", cmd_reindex),
    "reindex-ocr": ("OCR correction only", cmd_reindex_ocr),
    "reindex-embed": ("Rebuild embeddings only", cmd_reindex_embed),
    "poll": ("Process inbox (OCR + embed + classify)", cmd_poll),
}


def main() -> None:
    if len(sys.argv) < 2 or sys.argv[1] in ("-h", "--help"):
        print("Usage: python -m app.cli <command>\n")
        print("Commands:")
        for name, (desc, _) in COMMANDS.items():
            print(f"  {name:<20} {desc}")
        sys.exit(0 if len(sys.argv) >= 2 else 1)

    cmd_name = sys.argv[1]
    if cmd_name not in COMMANDS:
        print(f"Unknown command: {cmd_name}")
        print(f"Available: {', '.join(COMMANDS)}")
        sys.exit(1)

    _configure_logging()
    init_db()

    _, cmd_func = COMMANDS[cmd_name]
    asyncio.run(cmd_func())


if __name__ == "__main__":
    main()
