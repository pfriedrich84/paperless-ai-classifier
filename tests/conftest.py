"""Shared fixtures for the test suite."""

from __future__ import annotations

import os
import sqlite3
import tempfile
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from unittest.mock import AsyncMock

import pytest

# Set required env vars BEFORE importing app modules
os.environ.setdefault("PAPERLESS_URL", "http://test:8000")
os.environ.setdefault("PAPERLESS_TOKEN", "test-token")
os.environ.setdefault("PAPERLESS_INBOX_TAG_ID", "99")
os.environ.setdefault("DATA_DIR", tempfile.mkdtemp())

from app.db import EMBED_DIM, SCHEMA
from app.models import PaperlessDocument, PaperlessEntity


@pytest.fixture()
def tmp_db(tmp_path: Path) -> Path:
    """Create a temporary SQLite DB with the full schema applied (without sqlite-vec)."""
    db_path = tmp_path / "test.db"
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    # Strip virtual tables — they require extensions not available in tests
    # (vec0 requires sqlite-vec, FTS5 may not be compiled into all builds)
    # We keep all other tables including doc_embedding_meta
    import re

    schema_no_vec = re.sub(
        r"CREATE VIRTUAL TABLE IF NOT EXISTS doc_embeddings.*?;",
        "",
        SCHEMA,
        flags=re.DOTALL,
    )
    schema_no_vec = re.sub(
        r"CREATE VIRTUAL TABLE IF NOT EXISTS doc_fts.*?;",
        "",
        schema_no_vec,
        flags=re.DOTALL,
    )
    conn.executescript(schema_no_vec)
    conn.close()
    return db_path


@pytest.fixture()
def db_conn(tmp_db: Path) -> Iterator[sqlite3.Connection]:
    """Yield a connection to the temp DB."""
    conn = sqlite3.connect(str(tmp_db))
    conn.row_factory = sqlite3.Row
    try:
        yield conn
    finally:
        conn.close()


@contextmanager
def _mock_get_conn(db_path: Path) -> Iterator[sqlite3.Connection]:
    """Replacement for app.db.get_conn that uses the test DB."""
    conn = sqlite3.connect(str(db_path), isolation_level=None)
    conn.row_factory = sqlite3.Row
    try:
        yield conn
    finally:
        conn.close()


@pytest.fixture()
def patch_db(tmp_db: Path, monkeypatch: pytest.MonkeyPatch):
    """Monkeypatch get_conn to use the test database."""
    monkeypatch.setattr("app.db.get_conn", lambda: _mock_get_conn(tmp_db))
    # Also patch wherever get_conn is imported directly
    monkeypatch.setattr("app.worker.get_conn", lambda: _mock_get_conn(tmp_db))
    monkeypatch.setattr("app.pipeline.committer.get_conn", lambda: _mock_get_conn(tmp_db))
    monkeypatch.setattr("app.routes.review.get_conn", lambda: _mock_get_conn(tmp_db))
    monkeypatch.setattr("app.routes.correspondents.get_conn", lambda: _mock_get_conn(tmp_db))


@pytest.fixture()
def sample_entities() -> list[PaperlessEntity]:
    """A small set of entities for resolution tests."""
    return [
        PaperlessEntity(id=1, name="Max Mustermann"),
        PaperlessEntity(id=2, name="Stadtwerke München"),
        PaperlessEntity(id=3, name="Deutsche Post"),
        PaperlessEntity(id=10, name="Rechnung"),
        PaperlessEntity(id=11, name="Vertrag"),
        PaperlessEntity(id=20, name="Finanzen"),
        PaperlessEntity(id=21, name="Wohnung"),
    ]


@pytest.fixture()
def sample_correspondents() -> list[PaperlessEntity]:
    return [
        PaperlessEntity(id=1, name="Max Mustermann"),
        PaperlessEntity(id=2, name="Stadtwerke München"),
        PaperlessEntity(id=3, name="Deutsche Post"),
    ]


@pytest.fixture()
def sample_doctypes() -> list[PaperlessEntity]:
    return [
        PaperlessEntity(id=10, name="Rechnung"),
        PaperlessEntity(id=11, name="Vertrag"),
    ]


@pytest.fixture()
def sample_storage_paths() -> list[PaperlessEntity]:
    return [
        PaperlessEntity(id=30, name="Finanzen/Rechnungen"),
        PaperlessEntity(id=31, name="Vertraege"),
    ]


@pytest.fixture()
def sample_tags() -> list[PaperlessEntity]:
    return [
        PaperlessEntity(id=20, name="Finanzen"),
        PaperlessEntity(id=21, name="Wohnung"),
        PaperlessEntity(id=22, name="Strom"),
    ]


@pytest.fixture()
def sample_context_doc() -> PaperlessDocument:
    """A classified document suitable as context (not in inbox)."""
    return PaperlessDocument(
        id=5,
        title="Stromrechnung Q1 2024",
        content="Rechnung Nr. 2024-1234\nStadtwerke München GmbH\nStrom\n127,43 EUR\n15.03.2024",
        created_date="2024-03-15",
        correspondent=2,  # Stadtwerke München
        document_type=10,  # Rechnung
        storage_path=30,  # Finanzen/Rechnungen
        tags=[20, 22],  # Finanzen, Strom
    )


@pytest.fixture()
def sample_doc() -> PaperlessDocument:
    """A minimal test document."""
    return PaperlessDocument(
        id=42,
        title="Scan_2024-03-15.pdf",
        content="Rechnung Nr. 12345\nStadtwerke München GmbH\nBetrag: 87,50 EUR",
        tags=[99],  # inbox tag
    )


@pytest.fixture()
def mock_paperless() -> AsyncMock:
    """A mocked PaperlessClient."""
    client = AsyncMock()
    client.get_document = AsyncMock(
        return_value=PaperlessDocument(id=42, title="test", tags=[99, 5])
    )
    client.patch_document = AsyncMock(return_value=None)
    client.list_correspondents = AsyncMock(return_value=[])
    client.list_document_types = AsyncMock(return_value=[])
    client.list_storage_paths = AsyncMock(return_value=[])
    client.list_tags = AsyncMock(return_value=[])
    return client


@pytest.fixture()
def mock_ollama() -> AsyncMock:
    """A mocked OllamaClient."""
    client = AsyncMock()
    client.chat_json = AsyncMock(
        return_value={
            "title": "Stromrechnung März 2024",
            "date": "2024-03-15",
            "correspondent": "Stadtwerke München",
            "document_type": "Rechnung",
            "storage_path": None,
            "tags": [{"name": "Finanzen", "confidence": 90}],
            "confidence": 85,
            "reasoning": "Erkannt als Stromrechnung",
        }
    )
    client.embed = AsyncMock(return_value=[0.1] * EMBED_DIM)
    return client
