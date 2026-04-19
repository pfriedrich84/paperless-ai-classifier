"""CLI management commands for manual pipeline triggering.

Usage::

    python -m app.cli reindex          # Full reindex (OCR + embedding)
    python -m app.cli reindex-ocr      # OCR correction only (skip cached)
    python -m app.cli reindex-ocr --force  # OCR correction, ignore cache
    python -m app.cli reindex-embed    # Embedding only (skip OCR)
    python -m app.cli poll             # Process inbox (OCR + embed + classify)
    python -m app.cli poll --force     # Reprocess inbox docs (ignore idempotency skip)
    python -m app.cli process-doc 224  # Process one document by ID
    python -m app.cli process-doc 224 --force  # Reprocess one document
    python -m app.cli reset --yes      # Delete DB and recreate clean schema
    python -m app.cli reset --yes --include-config  # Also delete config.env
"""

from __future__ import annotations

import asyncio
import logging
import signal
import sys
from pathlib import Path

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


async def cmd_reindex_ocr(*, force: bool = False) -> None:
    """Run OCR correction on all Paperless documents (respects OCR_MODE)."""
    from app.pipeline.ocr_correction import batch_correct_documents, effective_ocr_mode

    mode = effective_ocr_mode()
    if mode == "off":
        print("OCR_MODE is 'off' — nothing to do. Set OCR_MODE to text/vision_light/vision_full.")
        return

    paperless = PaperlessClient()
    ollama = OllamaClient()
    try:
        corrected = await batch_correct_documents(paperless, ollama, force=force)
        print(f"OCR correction complete: {corrected} documents corrected (mode={mode}).")
    finally:
        await paperless.aclose()
        await ollama.aclose()


async def cmd_reindex_embed() -> None:
    """Rebuild embeddings only (skip OCR, use cached OCR text if available)."""
    from app.db import EMBED_DIM, get_conn
    from app.indexer import initial_index

    # Drop + recreate vec0 so dimension changes take effect, also clear FTS index
    with get_conn() as conn:
        conn.execute("DELETE FROM doc_embedding_meta")
        conn.execute("DROP TABLE IF EXISTS doc_embeddings")
        conn.execute(
            f"""CREATE VIRTUAL TABLE doc_embeddings USING vec0(
                document_id INTEGER PRIMARY KEY,
                embedding   FLOAT[{EMBED_DIM}]
            )"""
        )
        conn.execute("DELETE FROM doc_fts")
    print("Cleared existing embeddings and FTS index.")

    paperless = PaperlessClient()
    ollama = OllamaClient()
    try:
        count = await initial_index(paperless, ollama)
        print(f"Embedding complete: {count} documents indexed.")
    finally:
        await paperless.aclose()
        await ollama.aclose()


async def cmd_poll(*, force: bool = False) -> None:
    """Process inbox: OCR + embed + classify (same as scheduled poll).

    With ``force=True`` the idempotency skip check is bypassed.
    """
    from app.worker import poll_inbox

    paperless = PaperlessClient()
    ollama = OllamaClient()

    # The worker needs module-level client refs — set them via start_scheduler's pattern
    import app.worker as worker

    worker._paperless = paperless
    worker._ollama = ollama

    # Wire Ctrl+C to the worker's cooperative cancellation flag
    loop = asyncio.get_running_loop()
    loop.add_signal_handler(
        signal.SIGINT,
        lambda: (
            setattr(worker._poll_progress, "cancelled", True),
            print("\nInterrupting after current document… (press Ctrl+C again to force)"),
            loop.remove_signal_handler(signal.SIGINT),
        ),
    )

    try:
        await poll_inbox(force=force)
        if worker._poll_progress.cancelled:
            print("Inbox processing cancelled.")
        else:
            print("Inbox processing complete.")
    finally:
        await paperless.aclose()
        await ollama.aclose()


async def cmd_process_doc(document_id: int, *, force: bool = False) -> None:
    """Process exactly one document by ID (OCR + embed + classify)."""
    from app.db import get_conn
    from app.worker import _process_document

    paperless = PaperlessClient()
    ollama = OllamaClient()
    try:
        doc = await paperless.get_document(document_id)
        correspondents = await paperless.list_correspondents()
        doctypes = await paperless.list_document_types()
        storage_paths = await paperless.list_storage_paths()
        tags = await paperless.list_tags()

        if force:
            with get_conn() as conn:
                conn.execute("DELETE FROM processed_documents WHERE document_id = ?", (document_id,))

        result = await _process_document(
            doc,
            paperless,
            ollama,
            correspondents,
            doctypes,
            storage_paths,
            tags,
        )
        print(f"Document #{document_id} processing complete: {result}")
    finally:
        await paperless.aclose()
        await ollama.aclose()


def cmd_reset(include_config: bool = False) -> None:
    """Delete all persistent state and recreate a clean database."""
    log = structlog.get_logger("reset")
    data_dir = Path(settings.data_dir)
    db_path = settings.db_path

    # Build file list
    targets: list[Path] = [
        db_path,
        db_path.parent / f"{db_path.name}-wal",
        db_path.parent / f"{db_path.name}-shm",
    ]

    if include_config:
        targets.append(data_dir / "config.env")
        targets.extend(data_dir.glob("config.bak.*"))

    # Only existing files
    existing = [p for p in targets if p.exists()]

    if existing:
        print("Deleting:")
        for p in existing:
            print(f"  {p}")
    else:
        print("No existing state files found.")

    for p in existing:
        p.unlink()
        log.info("deleted", path=str(p))

    # Recreate clean DB
    init_db()
    print(f"Reset complete. Clean database created at {db_path}")


COMMANDS = {
    "reindex": ("Full reindex (OCR + embedding)", cmd_reindex),
    "reindex-ocr": ("OCR correction only (--force to ignore cache)", cmd_reindex_ocr),
    "reindex-embed": ("Rebuild embeddings only", cmd_reindex_embed),
    "poll": ("Process inbox (OCR + embed + classify, --force to reprocess)", cmd_poll),
    "process-doc": ("Process a single document by ID (optional --force)", cmd_process_doc),
    "reset": ("Delete all state and recreate empty DB (--yes required)", None),
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

    # reset is synchronous and must NOT call init_db() before deletion
    if cmd_name == "reset":
        extra_args = sys.argv[2:]
        if "--yes" not in extra_args:
            print("Safety check: pass --yes to confirm reset.")
            print("  archibot reset --yes")
            print("  archibot reset --yes --include-config")
            sys.exit(1)
        cmd_reset(include_config="--include-config" in extra_args)
        return

    init_db()

    extra_args = sys.argv[2:]
    force = "--force" in extra_args

    _, cmd_func = COMMANDS[cmd_name]
    try:
        if cmd_name in {"reindex-ocr", "poll"}:
            asyncio.run(cmd_func(force=force))
        elif cmd_name == "process-doc":
            doc_arg = next((a for a in extra_args if not a.startswith("-")), None)
            if doc_arg is None:
                print("Usage: archibot process-doc <document_id> [--force]")
                sys.exit(1)
            try:
                document_id = int(doc_arg)
            except ValueError:
                print(f"Invalid document_id: {doc_arg}")
                sys.exit(1)
            asyncio.run(cmd_func(document_id, force=force))
        else:
            asyncio.run(cmd_func())
    except KeyboardInterrupt:
        print("\nAborted.")
        sys.exit(130)


if __name__ == "__main__":
    main()
