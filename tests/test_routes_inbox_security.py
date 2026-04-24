"""Security-focused tests for inbox error handling."""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest

from app.db import init_db
from app.main import app, templates
from tests.conftest import bootstrap_csrf_client


@pytest.fixture(autouse=True)
def _setup_app(tmp_path, monkeypatch):
    monkeypatch.setattr("app.config.settings.data_dir", str(tmp_path))
    monkeypatch.setattr("app.config.settings.paperless_url", "http://test:8000")
    monkeypatch.setattr("app.config.settings.paperless_token", "test-token")
    monkeypatch.setattr("app.config.settings.paperless_inbox_tag_id", 99)

    init_db()

    mock_paperless = AsyncMock()
    mock_paperless.base_url = "http://test:8000"
    mock_paperless.list_inbox_documents = AsyncMock(side_effect=Exception("<b>boom</b>"))

    app.state.paperless = mock_paperless
    app.state.ollama = AsyncMock()
    app.state.templates = templates


@pytest.fixture()
def client():
    from starlette.testclient import TestClient

    return bootstrap_csrf_client(TestClient(app, raise_server_exceptions=True))


def test_process_inbox_hides_exception_details(client):
    r = client.post("/inbox/process-inbox")
    assert r.status_code == 500
    assert "Failed to fetch inbox. Check logs for details." in r.text
    assert "boom" not in r.text


def test_process_all_hides_exception_details(client):
    app.state.paperless.list_all_documents = AsyncMock(side_effect=Exception("<b>boom</b>"))
    r = client.post("/inbox/process-all")
    assert r.status_code == 500
    assert "Failed to fetch documents. Check logs for details." in r.text
    assert "boom" not in r.text
