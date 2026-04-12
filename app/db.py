"""SQLite setup with sqlite-vec extension and schema migrations."""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

import sqlite_vec
import structlog

from app.config import settings

log = structlog.get_logger(__name__)

# Embedding dimension for nomic-embed-text-v2-moe = 768. Change if you switch models.
EMBED_DIM = 768


SCHEMA = f"""
-- =========================================================================
-- Document processing state
-- =========================================================================
CREATE TABLE IF NOT EXISTS processed_documents (
    document_id     INTEGER PRIMARY KEY,
    last_updated_at TEXT NOT NULL,           -- ISO timestamp from Paperless
    last_processed  TEXT NOT NULL,           -- ISO timestamp of our run
    status          TEXT NOT NULL,           -- 'pending', 'committed', 'rejected', 'error'
    suggestion_id   INTEGER
);
CREATE INDEX IF NOT EXISTS idx_processed_status ON processed_documents(status);

-- =========================================================================
-- Suggestions from the LLM
-- =========================================================================
CREATE TABLE IF NOT EXISTS suggestions (
    id                      INTEGER PRIMARY KEY AUTOINCREMENT,
    document_id             INTEGER NOT NULL,
    created_at              TEXT NOT NULL DEFAULT (datetime('now')),
    status                  TEXT NOT NULL DEFAULT 'pending',  -- pending/accepted/rejected
    confidence              INTEGER,                           -- 0-100
    reasoning               TEXT,
    -- Original values from Paperless at the time of classification
    original_title          TEXT,
    original_date           TEXT,
    original_correspondent  INTEGER,
    original_doctype        INTEGER,
    original_storage_path   INTEGER,
    original_tags_json      TEXT,
    -- Proposed values (names/IDs; IDs only if resolved against existing entities)
    proposed_title          TEXT,
    proposed_date           TEXT,
    proposed_correspondent_name TEXT,
    proposed_correspondent_id   INTEGER,
    proposed_doctype_name       TEXT,
    proposed_doctype_id         INTEGER,
    proposed_storage_path_name  TEXT,
    proposed_storage_path_id    INTEGER,
    proposed_tags_json          TEXT,  -- list of {{name, id_if_known}}
    -- Raw
    raw_response            TEXT,
    context_docs_json       TEXT   -- JSON list of context docs used for classification
);
CREATE INDEX IF NOT EXISTS idx_suggestions_status ON suggestions(status);
CREATE INDEX IF NOT EXISTS idx_suggestions_doc    ON suggestions(document_id);

-- =========================================================================
-- Tag whitelist - new tags are staged here until explicitly approved
-- =========================================================================
CREATE TABLE IF NOT EXISTS tag_whitelist (
    name        TEXT PRIMARY KEY,
    paperless_id INTEGER,              -- set once created in Paperless
    approved    INTEGER NOT NULL DEFAULT 0,
    first_seen  TEXT NOT NULL DEFAULT (datetime('now')),
    times_seen  INTEGER NOT NULL DEFAULT 1,
    notes       TEXT
);

-- =========================================================================
-- Error log
-- =========================================================================
CREATE TABLE IF NOT EXISTS errors (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    occurred_at TEXT NOT NULL DEFAULT (datetime('now')),
    stage       TEXT NOT NULL,        -- poll/classify/commit/ocr/...
    document_id INTEGER,
    message     TEXT NOT NULL,
    details     TEXT
);
CREATE INDEX IF NOT EXISTS idx_errors_doc ON errors(document_id);

-- =========================================================================
-- Embeddings for context similarity (sqlite-vec virtual table)
-- =========================================================================
CREATE VIRTUAL TABLE IF NOT EXISTS doc_embeddings USING vec0(
    document_id INTEGER PRIMARY KEY,
    embedding   FLOAT[{EMBED_DIM}]
);

-- Metadata table shadowing doc_embeddings for human-readable lookups
CREATE TABLE IF NOT EXISTS doc_embedding_meta (
    document_id  INTEGER PRIMARY KEY,
    title        TEXT,
    correspondent INTEGER,
    doctype       INTEGER,
    indexed_at   TEXT NOT NULL DEFAULT (datetime('now'))
);

-- =========================================================================
-- Audit log
-- =========================================================================
CREATE TABLE IF NOT EXISTS audit_log (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    occurred_at TEXT NOT NULL DEFAULT (datetime('now')),
    action      TEXT NOT NULL,
    document_id INTEGER,
    actor       TEXT,
    details     TEXT
);
"""


def _connect(db_path: Path) -> sqlite3.Connection:
    """Open a connection with sqlite-vec loaded."""
    conn = sqlite3.connect(str(db_path), isolation_level=None, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.enable_load_extension(True)
    sqlite_vec.load(conn)
    conn.enable_load_extension(False)
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


_MIGRATIONS: list[tuple[str, str, str]] = [
    # (table, column, SQL) — applied if column does not exist yet
    (
        "suggestions",
        "context_docs_json",
        "ALTER TABLE suggestions ADD COLUMN context_docs_json TEXT",
    ),
]


def _migrate(conn: sqlite3.Connection) -> None:
    """Apply pending column migrations for existing databases."""
    for table, column, sql in _MIGRATIONS:
        cols = {row[1] for row in conn.execute(f"PRAGMA table_info({table})").fetchall()}
        if column not in cols:
            conn.execute(sql)
            log.info("migration applied", table=table, column=column)


def init_db() -> None:
    """Create the database file, apply the schema, and run migrations."""
    db_path = settings.db_path
    db_path.parent.mkdir(parents=True, exist_ok=True)
    log.info("initializing database", path=str(db_path))
    with _connect(db_path) as conn:
        conn.executescript(SCHEMA)
        _migrate(conn)
    log.info("database ready")


@contextmanager
def get_conn() -> Iterator[sqlite3.Connection]:
    """Context manager yielding a SQLite connection."""
    conn = _connect(settings.db_path)
    try:
        yield conn
    finally:
        conn.close()
