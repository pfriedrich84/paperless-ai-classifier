"""Tests for bulk approve/reject in the review queue."""

from __future__ import annotations

import json
from unittest.mock import AsyncMock

import pytest

from app.db import init_db
from app.main import app, templates
from app.models import PaperlessDocument
from tests.conftest import bootstrap_csrf_client


def _insert_suggestion(conn, sid, doc_id, *, status="pending", confidence=75):
    """Insert a minimal test suggestion into the DB."""
    conn.execute(
        """INSERT INTO suggestions
           (id, document_id, status, confidence,
            proposed_title, proposed_date,
            proposed_correspondent_id, proposed_doctype_id,
            proposed_storage_path_id, proposed_tags_json)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            sid,
            doc_id,
            status,
            confidence,
            f"Title for doc {doc_id}",
            "2025-01-15",
            2,  # correspondent
            10,  # doctype
            30,  # storage_path
            json.dumps([{"name": "Finanzen", "id": 20}]),
        ),
    )
    conn.commit()


@pytest.fixture(autouse=True)
def _setup_app(tmp_path, monkeypatch):
    monkeypatch.setattr("app.config.settings.data_dir", str(tmp_path))
    init_db()

    mock_paperless = AsyncMock()
    mock_paperless.base_url = "http://test:8000"
    mock_paperless.list_correspondents = AsyncMock(return_value=[])
    mock_paperless.list_document_types = AsyncMock(return_value=[])
    mock_paperless.list_storage_paths = AsyncMock(return_value=[])
    mock_paperless.list_tags = AsyncMock(return_value=[])
    mock_paperless.get_document = AsyncMock(
        return_value=PaperlessDocument(id=1, title="test", tags=[99])
    )
    mock_paperless.patch_document = AsyncMock(return_value=None)

    app.state.paperless = mock_paperless
    app.state.ollama = AsyncMock()
    app.state.templates = templates


@pytest.fixture()
def client():
    from starlette.testclient import TestClient

    return bootstrap_csrf_client(TestClient(app, raise_server_exceptions=True))


# ---------------------------------------------------------------------------
# Bulk Approve
# ---------------------------------------------------------------------------


class TestBulkApprove:
    def test_commits_selected(self, client, patch_db, db_conn):
        _insert_suggestion(db_conn, 1, 100)
        _insert_suggestion(db_conn, 2, 200)

        r = client.post("/review/bulk-approve", data={"suggestion_ids": ["1", "2"]})
        assert r.status_code == 200

        # Both should be committed
        rows = db_conn.execute("SELECT id, status FROM suggestions ORDER BY id").fetchall()
        assert [(row["id"], row["status"]) for row in rows] == [
            (1, "committed"),
            (2, "committed"),
        ]

        # Response should contain OOB fragments for both desktop and mobile
        assert 'id="suggestion-1"' in r.text
        assert 'id="suggestion-2"' in r.text
        assert 'id="suggestion-m-1"' in r.text
        assert 'id="suggestion-m-2"' in r.text
        assert "hx-swap-oob" in r.text

        # Toast header
        trigger = json.loads(r.headers["HX-Trigger"])
        assert "2 approved" in trigger["showToast"]["message"]
        assert trigger["showToast"]["type"] == "success"

    def test_empty_selection(self, client, patch_db):
        r = client.post("/review/bulk-approve", data={})
        assert r.status_code == 200
        trigger = json.loads(r.headers["HX-Trigger"])
        assert "No suggestions selected" in trigger["showToast"]["message"]

    def test_skips_non_pending(self, client, patch_db, db_conn):
        _insert_suggestion(db_conn, 1, 100, status="pending")
        _insert_suggestion(db_conn, 2, 200, status="committed")

        r = client.post("/review/bulk-approve", data={"suggestion_ids": ["1", "2"]})
        assert r.status_code == 200

        trigger = json.loads(r.headers["HX-Trigger"])
        assert "1 approved" in trigger["showToast"]["message"]
        assert "1 skipped" in trigger["showToast"]["message"]

        # Only ID 1 should be committed; ID 2 remains unchanged
        s1 = db_conn.execute("SELECT status FROM suggestions WHERE id=1").fetchone()
        s2 = db_conn.execute("SELECT status FROM suggestions WHERE id=2").fetchone()
        assert s1["status"] == "committed"
        assert s2["status"] == "committed"  # was already committed

    def test_partial_failure(self, client, patch_db, db_conn):
        _insert_suggestion(db_conn, 1, 100)
        _insert_suggestion(db_conn, 2, 200)

        # Make get_document fail for doc 200
        original_get = app.state.paperless.get_document

        async def get_doc_side_effect(doc_id):
            if doc_id == 200:
                raise RuntimeError("Paperless unreachable")
            return PaperlessDocument(id=doc_id, title="test", tags=[99])

        app.state.paperless.get_document = AsyncMock(side_effect=get_doc_side_effect)

        r = client.post("/review/bulk-approve", data={"suggestion_ids": ["1", "2"]})
        assert r.status_code == 200

        trigger = json.loads(r.headers["HX-Trigger"])
        msg = trigger["showToast"]["message"]
        assert "1 approved" in msg
        assert "1 failed" in msg
        assert trigger["showToast"]["type"] == "error"

        # OOB fragments should have correct styling
        assert "bg-green-50" in r.text
        assert "bg-red-50" in r.text

        app.state.paperless.get_document = original_get

    def test_uses_proposed_values(self, client, patch_db, db_conn):
        _insert_suggestion(db_conn, 1, 100)

        r = client.post("/review/bulk-approve", data={"suggestion_ids": ["1"]})
        assert r.status_code == 200

        # Verify patch_document was called with the proposed values
        call_args = app.state.paperless.patch_document.call_args
        doc_id_arg = call_args[0][0]
        fields_arg = call_args[0][1]
        assert doc_id_arg == 100
        assert fields_arg["title"] == "Title for doc 100"
        assert fields_arg["created_date"] == "2025-01-15"
        assert fields_arg["correspondent"] == 2
        assert fields_arg["document_type"] == 10
        assert fields_arg["storage_path"] == 30


# ---------------------------------------------------------------------------
# Bulk Reject
# ---------------------------------------------------------------------------


class TestBulkReject:
    def test_rejects_selected(self, client, patch_db, db_conn):
        _insert_suggestion(db_conn, 1, 100)
        _insert_suggestion(db_conn, 2, 200)

        r = client.post("/review/bulk-reject", data={"suggestion_ids": ["1", "2"]})
        assert r.status_code == 200

        rows = db_conn.execute("SELECT id, status FROM suggestions ORDER BY id").fetchall()
        assert [(row["id"], row["status"]) for row in rows] == [
            (1, "rejected"),
            (2, "rejected"),
        ]

        # Audit log entries
        audit = db_conn.execute("SELECT action FROM audit_log ORDER BY id").fetchall()
        assert len(audit) == 2
        assert all(row["action"] == "reject" for row in audit)

        # OOB fragments
        assert 'id="suggestion-1"' in r.text
        assert 'id="suggestion-m-2"' in r.text
        assert "hx-swap-oob" in r.text

        trigger = json.loads(r.headers["HX-Trigger"])
        assert "2 rejected" in trigger["showToast"]["message"]

    def test_empty_selection(self, client, patch_db):
        r = client.post("/review/bulk-reject", data={})
        assert r.status_code == 200
        trigger = json.loads(r.headers["HX-Trigger"])
        assert "No suggestions selected" in trigger["showToast"]["message"]

    def test_skips_non_pending(self, client, patch_db, db_conn):
        _insert_suggestion(db_conn, 1, 100, status="pending")
        _insert_suggestion(db_conn, 2, 200, status="rejected")

        r = client.post("/review/bulk-reject", data={"suggestion_ids": ["1", "2"]})
        assert r.status_code == 200

        trigger = json.loads(r.headers["HX-Trigger"])
        assert "1 rejected" in trigger["showToast"]["message"]
        assert "1 skipped" in trigger["showToast"]["message"]


# ---------------------------------------------------------------------------
# Template rendering
# ---------------------------------------------------------------------------


class TestReviewListTemplate:
    def test_renders_checkboxes(self, client, patch_db, db_conn):
        _insert_suggestion(db_conn, 1, 100)

        r = client.get("/review")
        assert r.status_code == 200
        assert 'class="bulk-check' in r.text
        assert 'id="select-all-desktop"' in r.text
        assert 'id="bulk-actions"' in r.text

    def test_high_confidence_pre_checked(self, client, patch_db, db_conn):
        _insert_suggestion(db_conn, 1, 100, confidence=90)
        _insert_suggestion(db_conn, 2, 200, confidence=50)

        r = client.get("/review")
        assert r.status_code == 200

        # Find the checkbox for suggestion 1 (90%) — should be checked
        # Find the checkbox for suggestion 2 (50%) — should not be checked
        # We check by looking at checkbox value + checked attribute proximity
        text = r.text
        idx_s1 = text.find('value="1"')
        idx_s2 = text.find('value="2"')
        assert idx_s1 != -1
        assert idx_s2 != -1

        # Look at a window around each checkbox for the "checked" attribute
        s1_context = text[max(0, idx_s1 - 50) : idx_s1 + 200]
        s2_context = text[max(0, idx_s2 - 50) : idx_s2 + 200]
        assert "checked" in s1_context
        assert "checked" not in s2_context
