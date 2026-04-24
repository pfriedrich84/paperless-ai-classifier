"""Tests for document type whitelist/blacklist routes."""

from __future__ import annotations

import sqlite3
from unittest.mock import AsyncMock

import pytest

from app.db import init_db
from app.main import app, templates
from tests.conftest import bootstrap_csrf_client


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

    return bootstrap_csrf_client(TestClient(app, raise_server_exceptions=True))


@pytest.fixture()
def db_path(tmp_path):
    """Return the path to the test database."""
    from app.config import settings

    return settings.db_path


class TestDoctypeRejectMovesToBlacklist:
    def test_reject_moves_to_blacklist(self, client, db_path):
        """POST /doctypes/X/reject should remove from whitelist and add to blacklist."""
        conn = sqlite3.connect(str(db_path))
        conn.row_factory = sqlite3.Row
        conn.execute("INSERT INTO doctype_whitelist (name, times_seen) VALUES ('Gutachten', 2)")
        conn.commit()
        conn.close()

        r = client.post("/doctypes/Gutachten/reject")
        assert r.status_code == 200

        conn = sqlite3.connect(str(db_path))
        conn.row_factory = sqlite3.Row
        wl = conn.execute("SELECT * FROM doctype_whitelist WHERE name = 'Gutachten'").fetchone()
        bl = conn.execute("SELECT * FROM doctype_blacklist WHERE name = 'Gutachten'").fetchone()
        conn.close()

        assert wl is None
        assert bl is not None

    def test_reject_preserves_times_seen(self, client, db_path):
        """times_seen should carry over from whitelist to blacklist."""
        conn = sqlite3.connect(str(db_path))
        conn.execute("INSERT INTO doctype_whitelist (name, times_seen) VALUES ('SeenType', 5)")
        conn.commit()
        conn.close()

        client.post("/doctypes/SeenType/reject")

        conn = sqlite3.connect(str(db_path))
        conn.row_factory = sqlite3.Row
        bl = conn.execute("SELECT * FROM doctype_blacklist WHERE name = 'SeenType'").fetchone()
        conn.close()

        assert bl["times_seen"] == 5

    def test_reject_creates_audit_log(self, client, db_path):
        """Rejecting a doctype should create an audit log entry."""
        conn = sqlite3.connect(str(db_path))
        conn.execute("INSERT INTO doctype_whitelist (name) VALUES ('AuditType')")
        conn.commit()
        conn.close()

        client.post("/doctypes/AuditType/reject")

        conn = sqlite3.connect(str(db_path))
        conn.row_factory = sqlite3.Row
        log = conn.execute("SELECT * FROM audit_log WHERE action = 'doctype_blacklist'").fetchone()
        conn.close()

        assert log is not None
        assert "AuditType" in log["details"]


class TestDoctypeApproveSecurity:
    def test_approve_hides_exception_details(self, client, db_path):
        app.state.paperless.create_document_type = AsyncMock(
            side_effect=Exception("<img src=x onerror=alert(1)>")
        )

        conn = sqlite3.connect(str(db_path))
        conn.execute(
            "INSERT INTO doctype_whitelist (name, times_seen) VALUES (?, ?)",
            ("<b>Invoice</b>", 1),
        )
        conn.commit()
        conn.close()

        r = client.post("/doctypes/%3Cb%3EInvoice%3C%2Fb%3E/approve")
        assert r.status_code == 500
        assert "Document type approval failed." in r.text
        assert "onerror" not in r.text

    def test_reject_handles_slash_in_name(self, client, db_path):
        conn = sqlite3.connect(str(db_path))
        conn.row_factory = sqlite3.Row
        conn.execute(
            "INSERT INTO doctype_whitelist (name, times_seen) VALUES (?, ?)",
            ("Forms / Tax", 2),
        )
        conn.commit()
        conn.close()

        r = client.post("/doctypes/Forms%20%2F%20Tax/reject")
        assert r.status_code == 200

        conn = sqlite3.connect(str(db_path))
        conn.row_factory = sqlite3.Row
        wl = conn.execute(
            "SELECT * FROM doctype_whitelist WHERE name = ?", ("Forms / Tax",)
        ).fetchone()
        bl = conn.execute(
            "SELECT * FROM doctype_blacklist WHERE name = ?", ("Forms / Tax",)
        ).fetchone()
        conn.close()

        assert wl is None
        assert bl is not None


class TestDoctypeUnblacklist:
    def test_unblacklist_removes_from_blacklist(self, client, db_path):
        """POST /doctypes/X/unblacklist should remove from blacklist."""
        conn = sqlite3.connect(str(db_path))
        conn.execute("INSERT INTO doctype_blacklist (name) VALUES ('BlockedType')")
        conn.commit()
        conn.close()

        r = client.post("/doctypes/BlockedType/unblacklist")
        assert r.status_code == 200

        conn = sqlite3.connect(str(db_path))
        row = conn.execute("SELECT * FROM doctype_blacklist WHERE name = 'BlockedType'").fetchone()
        conn.close()

        assert row is None

    def test_unblacklist_allows_reproposal(self, client, db_path, monkeypatch):
        """After unblacklist, _upsert_doctype_whitelist should insert again."""
        from tests.conftest import _mock_get_conn

        monkeypatch.setattr("app.worker.get_conn", lambda: _mock_get_conn(db_path))

        conn = sqlite3.connect(str(db_path))
        conn.execute("INSERT INTO doctype_blacklist (name) VALUES ('FreedType')")
        conn.commit()
        conn.close()

        client.post("/doctypes/FreedType/unblacklist")

        from app.worker import _upsert_doctype_whitelist

        _upsert_doctype_whitelist("FreedType")

        conn = sqlite3.connect(str(db_path))
        conn.row_factory = sqlite3.Row
        row = conn.execute("SELECT * FROM doctype_whitelist WHERE name = 'FreedType'").fetchone()
        conn.close()

        assert row is not None

    def test_unblacklist_creates_audit_log(self, client, db_path):
        """Unblacklisting a doctype should create an audit log entry."""
        conn = sqlite3.connect(str(db_path))
        conn.execute("INSERT INTO doctype_blacklist (name) VALUES ('UnblockType')")
        conn.commit()
        conn.close()

        client.post("/doctypes/UnblockType/unblacklist")

        conn = sqlite3.connect(str(db_path))
        conn.row_factory = sqlite3.Row
        log = conn.execute(
            "SELECT * FROM audit_log WHERE action = 'doctype_unblacklist'"
        ).fetchone()
        conn.close()

        assert log is not None
        assert "UnblockType" in log["details"]


class TestDoctypeWhitelistUpsert:
    """Test the worker-level _upsert_doctype_whitelist function."""

    def test_upsert_creates_new_entry(self, db_path, monkeypatch):
        from tests.conftest import _mock_get_conn

        monkeypatch.setattr("app.worker.get_conn", lambda: _mock_get_conn(db_path))

        from app.worker import _upsert_doctype_whitelist

        _upsert_doctype_whitelist("New Doctype")

        conn = sqlite3.connect(str(db_path))
        conn.row_factory = sqlite3.Row
        row = conn.execute("SELECT * FROM doctype_whitelist WHERE name = 'New Doctype'").fetchone()
        conn.close()

        assert row is not None
        assert row["times_seen"] == 1

    def test_upsert_increments_times_seen(self, db_path, monkeypatch):
        from tests.conftest import _mock_get_conn

        monkeypatch.setattr("app.worker.get_conn", lambda: _mock_get_conn(db_path))

        from app.worker import _upsert_doctype_whitelist

        _upsert_doctype_whitelist("Repeated Doctype")
        _upsert_doctype_whitelist("Repeated Doctype")

        conn = sqlite3.connect(str(db_path))
        conn.row_factory = sqlite3.Row
        row = conn.execute(
            "SELECT * FROM doctype_whitelist WHERE name = 'Repeated Doctype'"
        ).fetchone()
        conn.close()

        assert row["times_seen"] == 2

    def test_upsert_skips_blacklisted(self, db_path, monkeypatch):
        from tests.conftest import _mock_get_conn

        monkeypatch.setattr("app.worker.get_conn", lambda: _mock_get_conn(db_path))

        conn = sqlite3.connect(str(db_path))
        conn.execute("INSERT INTO doctype_blacklist (name) VALUES ('Blocked One')")
        conn.commit()
        conn.close()

        from app.worker import _upsert_doctype_whitelist

        _upsert_doctype_whitelist("Blocked One")

        conn = sqlite3.connect(str(db_path))
        row = conn.execute("SELECT * FROM doctype_whitelist WHERE name = 'Blocked One'").fetchone()
        conn.close()

        assert row is None

    def test_upsert_case_insensitive(self, db_path, monkeypatch):
        """Upsert should treat differing capitalization as the same doctype."""
        from tests.conftest import _mock_get_conn

        monkeypatch.setattr("app.worker.get_conn", lambda: _mock_get_conn(db_path))

        conn = sqlite3.connect(str(db_path))
        conn.row_factory = sqlite3.Row
        conn.execute("INSERT INTO doctype_whitelist (name, times_seen) VALUES ('Rechnung', 1)")
        conn.commit()
        conn.close()

        from app.worker import _upsert_doctype_whitelist

        _upsert_doctype_whitelist("rechnung")

        conn = sqlite3.connect(str(db_path))
        conn.row_factory = sqlite3.Row
        rows = conn.execute("SELECT name, times_seen FROM doctype_whitelist").fetchall()
        conn.close()

        assert len(rows) == 1
        assert rows[0]["name"] == "Rechnung"
        assert rows[0]["times_seen"] == 2
