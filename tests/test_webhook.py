"""Tests for webhook endpoints (/webhook/new + /webhook/edit)."""

from __future__ import annotations

import json
from unittest.mock import AsyncMock, patch

import pytest

from app.db import EMBED_DIM, init_db
from app.main import app, templates
from app.models import PaperlessDocument
from app.routes.webhook import _extract_document_id


# ---------------------------------------------------------------------------
# Unit tests for _extract_document_id
# ---------------------------------------------------------------------------
class TestExtractDocumentId:
    """Test flexible document_id extraction from various payload formats."""

    def test_workflow_format(self):
        body = {"event": "document_created", "object": {"id": 123, "title": "Test"}}
        assert _extract_document_id(body) == 123

    def test_workflow_format_updated(self):
        body = {"event": "document_updated", "object": {"id": 456}}
        assert _extract_document_id(body) == 456

    def test_legacy_format_int(self):
        assert _extract_document_id({"document_id": 42}) == 42

    def test_legacy_format_string(self):
        assert _extract_document_id({"document_id": "42"}) == 42

    def test_workflow_preferred_over_legacy(self):
        body = {"object": {"id": 10}, "document_id": 20}
        assert _extract_document_id(body) == 10

    def test_empty_payload(self):
        assert _extract_document_id({}) is None

    def test_object_without_id(self):
        assert _extract_document_id({"object": {"title": "x"}}) is None

    def test_invalid_id_type(self):
        assert _extract_document_id({"document_id": "abc"}) is None

    def test_object_id_string(self):
        body = {"object": {"id": "789"}}
        assert _extract_document_id(body) == 789


# ---------------------------------------------------------------------------
# Integration tests for webhook endpoints
# ---------------------------------------------------------------------------
_SAMPLE_DOC = PaperlessDocument(
    id=42,
    title="Test Document",
    content="Test content for embedding",
    tags=[99],
)


@pytest.fixture(autouse=True)
def _setup_app(tmp_path, monkeypatch):
    """Initialize the app with a temp DB and mocked clients."""
    monkeypatch.setattr("app.config.settings.data_dir", str(tmp_path))
    monkeypatch.setattr("app.config.settings.webhook_secret", "")

    init_db()

    mock_paperless = AsyncMock()
    mock_paperless.base_url = "http://test:8000"
    mock_paperless.get_document = AsyncMock(return_value=_SAMPLE_DOC)
    mock_paperless.list_correspondents = AsyncMock(return_value=[])
    mock_paperless.list_document_types = AsyncMock(return_value=[])
    mock_paperless.list_storage_paths = AsyncMock(return_value=[])
    mock_paperless.list_tags = AsyncMock(return_value=[])

    mock_ollama = AsyncMock()
    mock_ollama.embed = AsyncMock(return_value=[0.1] * EMBED_DIM)

    app.state.paperless = mock_paperless
    app.state.ollama = mock_ollama
    app.state.templates = templates


@pytest.fixture()
def client():
    from starlette.testclient import TestClient

    return TestClient(app, raise_server_exceptions=False)


# ---------------------------------------------------------------------------
# POST /webhook/new — full processing
# ---------------------------------------------------------------------------
class TestWebhookNew:
    """Full-processing webhook endpoint."""

    @patch("app.routes.webhook._process_document", new_callable=AsyncMock)
    def test_legacy_format(self, mock_process, client):
        r = client.post("/webhook/new", json={"document_id": 42})
        assert r.status_code == 200
        assert r.json()["status"] == "ok"
        mock_process.assert_awaited_once()

    @patch("app.routes.webhook._process_document", new_callable=AsyncMock)
    def test_workflow_format(self, mock_process, client):
        payload = {"event": "document_created", "object": {"id": 42, "title": "Test"}}
        r = client.post("/webhook/new", json=payload)
        assert r.status_code == 200
        assert r.json()["status"] == "ok"
        mock_process.assert_awaited_once()

    def test_missing_document_id(self, client):
        r = client.post("/webhook/new", json={"foo": "bar"})
        assert r.status_code == 422

    @patch("app.routes.webhook._process_document", new_callable=AsyncMock)
    def test_auth_required(self, mock_process, client, monkeypatch):
        monkeypatch.setattr("app.config.settings.webhook_secret", "my-secret")
        r = client.post("/webhook/new", json={"document_id": 42})
        assert r.status_code == 403
        mock_process.assert_not_awaited()

    @patch("app.routes.webhook._process_document", new_callable=AsyncMock)
    def test_auth_success(self, mock_process, client, monkeypatch):
        monkeypatch.setattr("app.config.settings.webhook_secret", "my-secret")
        r = client.post(
            "/webhook/new",
            json={"document_id": 42},
            headers={"X-Webhook-Secret": "my-secret"},
        )
        assert r.status_code == 200
        mock_process.assert_awaited_once()

    @patch("app.routes.webhook.is_reindexing", return_value=True)
    def test_reindex_guard(self, _mock_reindex, client):
        r = client.post("/webhook/new", json={"document_id": 42})
        assert r.status_code == 503


# ---------------------------------------------------------------------------
# POST /webhook/edit — embedding-only
# ---------------------------------------------------------------------------
class TestWebhookEdit:
    """Embedding-only webhook endpoint."""

    @patch("app.routes.webhook.maybe_correct_ocr", new_callable=AsyncMock, return_value=("text", 0))
    @patch("app.routes.webhook.context_builder")
    def test_workflow_format(self, mock_cb, _mock_ocr, client):
        mock_cb.document_summary.return_value = "Test summary"
        mock_cb.store_embedding = AsyncMock()
        payload = {"event": "document_updated", "object": {"id": 42}}
        r = client.post("/webhook/edit", json=payload)
        assert r.status_code == 200
        data = r.json()
        assert data["status"] == "ok"
        assert data["action"] == "reembedded"
        mock_cb.store_embedding.assert_called_once()

    @patch("app.routes.webhook.maybe_correct_ocr", new_callable=AsyncMock, return_value=("text", 0))
    @patch("app.routes.webhook.context_builder")
    def test_legacy_format(self, mock_cb, _mock_ocr, client):
        mock_cb.document_summary.return_value = "Test summary"
        mock_cb.store_embedding = AsyncMock()
        r = client.post("/webhook/edit", json={"document_id": 42})
        assert r.status_code == 200
        assert r.json()["action"] == "reembedded"
        mock_cb.store_embedding.assert_called_once()

    def test_missing_document_id(self, client):
        r = client.post("/webhook/edit", json={"foo": "bar"})
        assert r.status_code == 422

    @patch("app.routes.webhook.maybe_correct_ocr", new_callable=AsyncMock, return_value=("text", 0))
    @patch("app.routes.webhook.context_builder")
    def test_auth_required(self, mock_cb, _mock_ocr, client, monkeypatch):
        monkeypatch.setattr("app.config.settings.webhook_secret", "my-secret")
        r = client.post("/webhook/edit", json={"document_id": 42})
        assert r.status_code == 403
        mock_cb.store_embedding.assert_not_called()

    @patch("app.routes.webhook.maybe_correct_ocr", new_callable=AsyncMock, return_value=("text", 0))
    @patch("app.routes.webhook.context_builder")
    def test_auth_success(self, mock_cb, _mock_ocr, client, monkeypatch):
        monkeypatch.setattr("app.config.settings.webhook_secret", "my-secret")
        mock_cb.document_summary.return_value = "Test summary"
        mock_cb.store_embedding = AsyncMock()
        r = client.post(
            "/webhook/edit",
            json={"document_id": 42},
            headers={"X-Webhook-Secret": "my-secret"},
        )
        assert r.status_code == 200
        assert r.json()["action"] == "reembedded"

    @patch("app.routes.webhook.is_reindexing", return_value=True)
    def test_reindex_guard(self, _mock_reindex, client):
        r = client.post("/webhook/edit", json={"document_id": 42})
        assert r.status_code == 503

    @patch("app.routes.webhook.maybe_correct_ocr", new_callable=AsyncMock, return_value=("text", 0))
    @patch("app.routes.webhook.context_builder")
    def test_empty_summary_skipped(self, mock_cb, _mock_ocr, client):
        mock_cb.document_summary.return_value = "   "
        r = client.post("/webhook/edit", json={"document_id": 42})
        assert r.status_code == 200
        assert r.json()["action"] == "skipped_empty"
        mock_cb.store_embedding.assert_not_called()

    def test_get_embeddings_still_serves_dashboard(self, client):
        """GET /embeddings should still return the dashboard."""
        r = client.get("/embeddings")
        assert r.status_code == 200
        assert "Embeddings" in r.text


# ---------------------------------------------------------------------------
# Multipart/form-data tests (Paperless "include document" option)
# ---------------------------------------------------------------------------
class TestWebhookMultipart:
    """Test webhook endpoints with multipart/form-data payloads.

    When Paperless-NGX has "Dokument einbeziehen" enabled, webhooks send
    multipart/form-data with the PDF attached instead of plain JSON.
    """

    @patch("app.routes.webhook._process_document", new_callable=AsyncMock)
    def test_new_multipart_with_json_field(self, mock_process, client):
        """webhook/new should parse document_id from a JSON form field."""
        payload = json.dumps({"event": "document_created", "object": {"id": 42}})
        r = client.post(
            "/webhook/new",
            data={"payload": payload},
            files={"document": ("test.pdf", b"%PDF-1.4 fake content", "application/pdf")},
        )
        assert r.status_code == 200
        assert r.json()["status"] == "ok"
        mock_process.assert_awaited_once()

    @patch("app.routes.webhook.maybe_correct_ocr", new_callable=AsyncMock, return_value=("text", 0))
    @patch("app.routes.webhook.context_builder")
    def test_edit_multipart_with_json_field(self, mock_cb, _mock_ocr, client):
        """webhook/edit should parse document_id from a JSON form field."""
        mock_cb.document_summary.return_value = "Test summary"
        mock_cb.store_embedding = AsyncMock()
        payload = json.dumps({"event": "document_updated", "object": {"id": 42}})
        r = client.post(
            "/webhook/edit",
            data={"payload": payload},
            files={"document": ("test.pdf", b"%PDF-1.4 fake content", "application/pdf")},
        )
        assert r.status_code == 200
        assert r.json()["action"] == "reembedded"

    @patch("app.routes.webhook._process_document", new_callable=AsyncMock)
    def test_new_multipart_plain_fields(self, mock_process, client):
        """webhook/new should handle plain form fields (document_id as string)."""
        r = client.post(
            "/webhook/new",
            data={"document_id": "42"},
            files={"document": ("test.pdf", b"%PDF-1.4 fake", "application/pdf")},
        )
        assert r.status_code == 200
        assert r.json()["status"] == "ok"

    def test_edit_multipart_no_document_id(self, client):
        """webhook/edit should return 422 when multipart has no document_id."""
        r = client.post(
            "/webhook/edit",
            data={"irrelevant": "data"},
            files={"document": ("test.pdf", b"%PDF-1.4 fake", "application/pdf")},
        )
        assert r.status_code == 422

    @patch("app.routes.webhook._process_document", new_callable=AsyncMock)
    def test_new_multipart_binary_pdf_no_crash(self, mock_process, client):
        """webhook/new must not crash on binary PDF data with invalid UTF-8."""
        # Simulate the exact bytes that caused the UnicodeDecodeError
        binary_pdf = b"%PDF-1.4\xff\xd9\xda\x00\x01\x02 binary JBIG2 data"
        payload = json.dumps({"document_id": 42})
        r = client.post(
            "/webhook/new",
            data={"payload": payload},
            files={"document": ("scan.pdf", binary_pdf, "application/pdf")},
        )
        assert r.status_code == 200
        assert r.json()["status"] == "ok"
