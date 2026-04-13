"""Tests for tag whitelist/blacklist routes."""

from __future__ import annotations

import sqlite3
from unittest.mock import AsyncMock

import pytest

from app.db import init_db
from app.main import app, templates


@pytest.fixture(autouse=True)
def _setup_app(tmp_path, monkeypatch):
    """Initialize the app with a temp DB and mocked clients."""
    monkeypatch.setattr("app.config.settings.data_dir", str(tmp_path))

    init_db()

    mock_paperless = AsyncMock()
    mock_paperless.base_url = "http://test:8000"
    mock_paperless.list_correspondents = AsyncMock(return_value=[])
    mock_paperless.list_document_types = AsyncMock(return_value=[])
    mock_paperless.list_storage_paths = AsyncMock(return_value=[])
    mock_paperless.list_tags = AsyncMock(return_value=[])

    app.state.paperless = mock_paperless
    app.state.ollama = AsyncMock()
    app.state.templates = templates


@pytest.fixture()
def client():
    from starlette.testclient import TestClient

    return TestClient(app, raise_server_exceptions=True)


@pytest.fixture()
def db_path(tmp_path):
    """Return the path to the test database."""
    from app.config import settings

    return settings.db_path


class TestRejectMovesToBlacklist:
    def test_reject_moves_to_blacklist(self, client, db_path):
        """POST /tags/X/reject should remove from whitelist and add to blacklist."""
        conn = sqlite3.connect(str(db_path))
        conn.row_factory = sqlite3.Row
        conn.execute("INSERT INTO tag_whitelist (name, times_seen) VALUES ('TestTag', 2)")
        conn.commit()
        conn.close()

        r = client.post("/tags/TestTag/reject")
        assert r.status_code == 200

        conn = sqlite3.connect(str(db_path))
        conn.row_factory = sqlite3.Row
        wl = conn.execute("SELECT * FROM tag_whitelist WHERE name = 'TestTag'").fetchone()
        bl = conn.execute("SELECT * FROM tag_blacklist WHERE name = 'TestTag'").fetchone()
        conn.close()

        assert wl is None
        assert bl is not None

    def test_reject_preserves_times_seen(self, client, db_path):
        """times_seen should carry over from whitelist to blacklist."""
        conn = sqlite3.connect(str(db_path))
        conn.execute("INSERT INTO tag_whitelist (name, times_seen) VALUES ('SeenTag', 5)")
        conn.commit()
        conn.close()

        client.post("/tags/SeenTag/reject")

        conn = sqlite3.connect(str(db_path))
        conn.row_factory = sqlite3.Row
        bl = conn.execute("SELECT * FROM tag_blacklist WHERE name = 'SeenTag'").fetchone()
        conn.close()

        assert bl["times_seen"] == 5

    def test_reject_creates_audit_log(self, client, db_path):
        """Rejecting a tag should create an audit log entry with action='tag_blacklist'."""
        conn = sqlite3.connect(str(db_path))
        conn.execute("INSERT INTO tag_whitelist (name) VALUES ('AuditTag')")
        conn.commit()
        conn.close()

        client.post("/tags/AuditTag/reject")

        conn = sqlite3.connect(str(db_path))
        conn.row_factory = sqlite3.Row
        log = conn.execute("SELECT * FROM audit_log WHERE action = 'tag_blacklist'").fetchone()
        conn.close()

        assert log is not None
        assert "AuditTag" in log["details"]


class TestUnblacklist:
    def test_unblacklist_removes_from_blacklist(self, client, db_path):
        """POST /tags/X/unblacklist should remove the tag from blacklist."""
        conn = sqlite3.connect(str(db_path))
        conn.execute("INSERT INTO tag_blacklist (name) VALUES ('BlockedTag')")
        conn.commit()
        conn.close()

        r = client.post("/tags/BlockedTag/unblacklist")
        assert r.status_code == 200

        conn = sqlite3.connect(str(db_path))
        row = conn.execute("SELECT * FROM tag_blacklist WHERE name = 'BlockedTag'").fetchone()
        conn.close()

        assert row is None

    def test_unblacklist_allows_reproposal(self, client, db_path, monkeypatch):
        """After unblacklist, _upsert_tag_whitelist should be able to insert the tag again."""
        from tests.conftest import _mock_get_conn

        monkeypatch.setattr("app.worker.get_conn", lambda: _mock_get_conn(db_path))

        conn = sqlite3.connect(str(db_path))
        conn.execute("INSERT INTO tag_blacklist (name) VALUES ('FreedTag')")
        conn.commit()
        conn.close()

        # Unblacklist
        client.post("/tags/FreedTag/unblacklist")

        # Now _upsert_tag_whitelist should work
        from app.worker import _upsert_tag_whitelist

        _upsert_tag_whitelist("FreedTag")

        conn = sqlite3.connect(str(db_path))
        conn.row_factory = sqlite3.Row
        row = conn.execute("SELECT * FROM tag_whitelist WHERE name = 'FreedTag'").fetchone()
        conn.close()

        assert row is not None

    def test_unblacklist_creates_audit_log(self, client, db_path):
        """Unblacklisting a tag should create an audit log entry."""
        conn = sqlite3.connect(str(db_path))
        conn.execute("INSERT INTO tag_blacklist (name) VALUES ('UnblockTag')")
        conn.commit()
        conn.close()

        client.post("/tags/UnblockTag/unblacklist")

        conn = sqlite3.connect(str(db_path))
        conn.row_factory = sqlite3.Row
        log = conn.execute("SELECT * FROM audit_log WHERE action = 'tag_unblacklist'").fetchone()
        conn.close()

        assert log is not None
        assert "UnblockTag" in log["details"]
