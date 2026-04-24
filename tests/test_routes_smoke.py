"""Smoke tests for all GUI routes — ensures templates render without errors."""

from __future__ import annotations

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


class TestRouteSmoke:
    """Every GET route should return 200 (or 404 for missing), never 500."""

    def test_dashboard(self, client):
        r = client.get("/")
        assert r.status_code == 200
        assert "Übersicht" in r.text

    def test_review_list(self, client):
        r = client.get("/review")
        assert r.status_code == 200

    def test_review_detail_not_found(self, client):
        r = client.get("/review/99999")
        assert r.status_code == 404

    def test_approvals_entry_redirects(self, client):
        r = client.get("/approvals", follow_redirects=False)
        assert r.status_code == 302
        assert r.headers["location"] == "/tags"

    def test_tags(self, client):
        r = client.get("/tags")
        assert r.status_code == 200
        assert "Freigaben" in r.text
        assert "Korrespondenten" in r.text
        assert "Dokumenttypen" in r.text

    def test_errors(self, client):
        r = client.get("/errors")
        assert r.status_code == 200

    def test_stats(self, client):
        r = client.get("/stats")
        assert r.status_code == 200

    def test_settings(self, client):
        r = client.get("/settings")
        assert r.status_code == 200

    def test_chat(self, client):
        r = client.get("/chat")
        assert r.status_code == 200
        assert "Chat" in r.text

    def test_embeddings(self, client):
        r = client.get("/embeddings")
        assert r.status_code == 200
        assert "Embeddings" in r.text

    def test_embeddings_search(self, client):
        r = client.get("/embeddings/search")
        assert r.status_code == 200

    def test_healthz(self, client):
        r = client.get("/healthz")
        assert r.status_code == 200
        assert r.json() == {"status": "ok"}
