"""Tests for correspondent whitelist/blacklist routes."""

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


class TestCorrespondentRejectMovesToBlacklist:
    def test_reject_moves_to_blacklist(self, client, db_path):
        """POST /correspondents/X/reject should remove from whitelist and add to blacklist."""
        conn = sqlite3.connect(str(db_path))
        conn.row_factory = sqlite3.Row
        conn.execute(
            "INSERT INTO correspondent_whitelist (name, times_seen) VALUES ('Dr. Dagmar Prinz', 2)"
        )
        conn.commit()
        conn.close()

        r = client.post("/correspondents/Dr. Dagmar Prinz/reject")
        assert r.status_code == 200

        conn = sqlite3.connect(str(db_path))
        conn.row_factory = sqlite3.Row
        wl = conn.execute(
            "SELECT * FROM correspondent_whitelist WHERE name = 'Dr. Dagmar Prinz'"
        ).fetchone()
        bl = conn.execute(
            "SELECT * FROM correspondent_blacklist WHERE name = 'Dr. Dagmar Prinz'"
        ).fetchone()
        conn.close()

        assert wl is None
        assert bl is not None

    def test_reject_preserves_times_seen(self, client, db_path):
        """times_seen should carry over from whitelist to blacklist."""
        conn = sqlite3.connect(str(db_path))
        conn.execute(
            "INSERT INTO correspondent_whitelist (name, times_seen) VALUES ('SeenCorr', 5)"
        )
        conn.commit()
        conn.close()

        client.post("/correspondents/SeenCorr/reject")

        conn = sqlite3.connect(str(db_path))
        conn.row_factory = sqlite3.Row
        bl = conn.execute(
            "SELECT * FROM correspondent_blacklist WHERE name = 'SeenCorr'"
        ).fetchone()
        conn.close()

        assert bl["times_seen"] == 5

    def test_reject_handles_slash_in_name(self, client, db_path):
        """Names containing '/' should still be routed and rejected correctly."""
        conn = sqlite3.connect(str(db_path))
        conn.row_factory = sqlite3.Row
        conn.execute(
            "INSERT INTO correspondent_whitelist (name, times_seen) VALUES (?, ?)",
            ("TK / Die Techniker", 3),
        )
        conn.commit()
        conn.close()

        r = client.post("/correspondents/TK%20%2F%20Die%20Techniker/reject")
        assert r.status_code == 200

        conn = sqlite3.connect(str(db_path))
        conn.row_factory = sqlite3.Row
        wl = conn.execute(
            "SELECT * FROM correspondent_whitelist WHERE name = ?", ("TK / Die Techniker",)
        ).fetchone()
        bl = conn.execute(
            "SELECT * FROM correspondent_blacklist WHERE name = ?", ("TK / Die Techniker",)
        ).fetchone()
        conn.close()

        assert wl is None
        assert bl is not None

    def test_reject_creates_audit_log(self, client, db_path):
        """Rejecting a correspondent should create an audit log entry."""
        conn = sqlite3.connect(str(db_path))
        conn.execute("INSERT INTO correspondent_whitelist (name) VALUES ('AuditCorr')")
        conn.commit()
        conn.close()

        client.post("/correspondents/AuditCorr/reject")

        conn = sqlite3.connect(str(db_path))
        conn.row_factory = sqlite3.Row
        log = conn.execute(
            "SELECT * FROM audit_log WHERE action = 'correspondent_blacklist'"
        ).fetchone()
        conn.close()

        assert log is not None
        assert "AuditCorr" in log["details"]


class TestCorrespondentApprove:
    def test_approve_handles_slash_in_name(self, client, db_path):
        """Names containing '/' should still be routed and approved correctly."""
        app.state.paperless.create_correspondent = AsyncMock(return_value=type("E", (), {"id": 123})())

        conn = sqlite3.connect(str(db_path))
        conn.execute(
            "INSERT INTO correspondent_whitelist (name, times_seen) VALUES (?, ?)",
            ("AOK / Rheinland", 2),
        )
        conn.commit()
        conn.close()

        r = client.post("/correspondents/AOK%20%2F%20Rheinland/approve")
        assert r.status_code == 200

        conn = sqlite3.connect(str(db_path))
        conn.row_factory = sqlite3.Row
        wl = conn.execute(
            "SELECT approved, paperless_id FROM correspondent_whitelist WHERE name = ?",
            ("AOK / Rheinland",),
        ).fetchone()
        conn.close()

        assert wl is not None
        assert wl["approved"] == 1
        assert wl["paperless_id"] == 123


class TestCorrespondentUnblacklist:
    def test_unblacklist_removes_from_blacklist(self, client, db_path):
        """POST /correspondents/X/unblacklist should remove from blacklist."""
        conn = sqlite3.connect(str(db_path))
        conn.execute("INSERT INTO correspondent_blacklist (name) VALUES ('BlockedCorr')")
        conn.commit()
        conn.close()

        r = client.post("/correspondents/BlockedCorr/unblacklist")
        assert r.status_code == 200

        conn = sqlite3.connect(str(db_path))
        row = conn.execute(
            "SELECT * FROM correspondent_blacklist WHERE name = 'BlockedCorr'"
        ).fetchone()
        conn.close()

        assert row is None

    def test_unblacklist_allows_reproposal(self, client, db_path, monkeypatch):
        """After unblacklist, _upsert_correspondent_whitelist should insert again."""
        from tests.conftest import _mock_get_conn

        monkeypatch.setattr("app.worker.get_conn", lambda: _mock_get_conn(db_path))

        conn = sqlite3.connect(str(db_path))
        conn.execute("INSERT INTO correspondent_blacklist (name) VALUES ('FreedCorr')")
        conn.commit()
        conn.close()

        # Unblacklist
        client.post("/correspondents/FreedCorr/unblacklist")

        # Now _upsert_correspondent_whitelist should work
        from app.worker import _upsert_correspondent_whitelist

        _upsert_correspondent_whitelist("FreedCorr")

        conn = sqlite3.connect(str(db_path))
        conn.row_factory = sqlite3.Row
        row = conn.execute(
            "SELECT * FROM correspondent_whitelist WHERE name = 'FreedCorr'"
        ).fetchone()
        conn.close()

        assert row is not None

    def test_unblacklist_creates_audit_log(self, client, db_path):
        """Unblacklisting a correspondent should create an audit log entry."""
        conn = sqlite3.connect(str(db_path))
        conn.execute("INSERT INTO correspondent_blacklist (name) VALUES ('UnblockCorr')")
        conn.commit()
        conn.close()

        client.post("/correspondents/UnblockCorr/unblacklist")

        conn = sqlite3.connect(str(db_path))
        conn.row_factory = sqlite3.Row
        log = conn.execute(
            "SELECT * FROM audit_log WHERE action = 'correspondent_unblacklist'"
        ).fetchone()
        conn.close()

        assert log is not None
        assert "UnblockCorr" in log["details"]


class TestCorrespondentWhitelistUpsert:
    """Test the worker-level _upsert_correspondent_whitelist function."""

    def test_upsert_creates_new_entry(self, db_path, monkeypatch):
        from tests.conftest import _mock_get_conn

        monkeypatch.setattr("app.worker.get_conn", lambda: _mock_get_conn(db_path))

        from app.worker import _upsert_correspondent_whitelist

        _upsert_correspondent_whitelist("New Correspondent")

        conn = sqlite3.connect(str(db_path))
        conn.row_factory = sqlite3.Row
        row = conn.execute(
            "SELECT * FROM correspondent_whitelist WHERE name = 'New Correspondent'"
        ).fetchone()
        conn.close()

        assert row is not None
        assert row["times_seen"] == 1

    def test_upsert_increments_times_seen(self, db_path, monkeypatch):
        from tests.conftest import _mock_get_conn

        monkeypatch.setattr("app.worker.get_conn", lambda: _mock_get_conn(db_path))

        from app.worker import _upsert_correspondent_whitelist

        _upsert_correspondent_whitelist("Repeated Correspondent")
        _upsert_correspondent_whitelist("Repeated Correspondent")

        conn = sqlite3.connect(str(db_path))
        conn.row_factory = sqlite3.Row
        row = conn.execute(
            "SELECT * FROM correspondent_whitelist WHERE name = 'Repeated Correspondent'"
        ).fetchone()
        conn.close()

        assert row["times_seen"] == 2

    def test_upsert_skips_blacklisted(self, db_path, monkeypatch):
        from tests.conftest import _mock_get_conn

        monkeypatch.setattr("app.worker.get_conn", lambda: _mock_get_conn(db_path))

        conn = sqlite3.connect(str(db_path))
        conn.execute("INSERT INTO correspondent_blacklist (name) VALUES ('Blocked One')")
        conn.commit()
        conn.close()

        from app.worker import _upsert_correspondent_whitelist

        _upsert_correspondent_whitelist("Blocked One")

        conn = sqlite3.connect(str(db_path))
        row = conn.execute(
            "SELECT * FROM correspondent_whitelist WHERE name = 'Blocked One'"
        ).fetchone()
        conn.close()

        assert row is None

    def test_upsert_case_insensitive(self, db_path, monkeypatch):
        """Upsert should treat differing capitalization as the same correspondent."""
        from tests.conftest import _mock_get_conn

        monkeypatch.setattr("app.worker.get_conn", lambda: _mock_get_conn(db_path))

        conn = sqlite3.connect(str(db_path))
        conn.row_factory = sqlite3.Row
        conn.execute("INSERT INTO correspondent_whitelist (name, times_seen) VALUES ('TK', 1)")
        conn.commit()
        conn.close()

        from app.worker import _upsert_correspondent_whitelist

        _upsert_correspondent_whitelist("tk")

        conn = sqlite3.connect(str(db_path))
        conn.row_factory = sqlite3.Row
        rows = conn.execute("SELECT name, times_seen FROM correspondent_whitelist").fetchall()
        conn.close()

        assert len(rows) == 1
        assert rows[0]["name"] == "TK"
        assert rows[0]["times_seen"] == 2
