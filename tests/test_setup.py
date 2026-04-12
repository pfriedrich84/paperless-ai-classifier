"""Tests for the onboarding wizard and settings config-save routes."""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest

from app.db import init_db
from app.main import app, templates
from app.models import PaperlessEntity


@pytest.fixture(autouse=True)
def _setup_app(tmp_path, monkeypatch):
    """Initialise the app with a temp DB and mocked clients for all tests."""
    monkeypatch.setattr("app.config.settings.data_dir", str(tmp_path))
    # Ensure the app thinks it's configured (non-empty URL/token/tag)
    monkeypatch.setattr("app.config.settings.paperless_url", "http://test:8000")
    monkeypatch.setattr("app.config.settings.paperless_token", "test-token")
    monkeypatch.setattr("app.config.settings.paperless_inbox_tag_id", 99)

    init_db()

    mock_paperless = AsyncMock()
    mock_paperless.base_url = "http://test:8000"
    mock_paperless.list_correspondents = AsyncMock(return_value=[])
    mock_paperless.list_document_types = AsyncMock(return_value=[])
    mock_paperless.list_storage_paths = AsyncMock(return_value=[])
    mock_paperless.list_tags = AsyncMock(return_value=[])

    app.state.paperless = mock_paperless
    app.state.ollama = AsyncMock()
    app.state.telegram = AsyncMock()
    app.state.templates = templates


@pytest.fixture()
def client():
    from starlette.testclient import TestClient

    return TestClient(app, raise_server_exceptions=True)


# ---------------------------------------------------------------------------
# Setup wizard routes
# ---------------------------------------------------------------------------
class TestSetupWizard:
    def test_setup_page_renders(self, client):
        r = client.get("/setup")
        assert r.status_code == 200
        assert "Setup Wizard" in r.text
        assert "Paperless-NGX Connection" in r.text

    def test_setup_prefills_from_env(self, client, monkeypatch):
        """When env vars are set, the wizard fields should be pre-filled."""
        monkeypatch.setattr("app.config.settings.paperless_url", "http://my-paperless:8000")
        monkeypatch.setattr("app.config.settings.paperless_token", "my-secret-tok")
        monkeypatch.setattr("app.config.settings.ollama_url", "http://my-ollama:11434")
        monkeypatch.setattr("app.config.settings.ollama_model", "llama3:8b")
        r = client.get("/setup")
        assert r.status_code == 200
        assert "http://my-paperless:8000" in r.text
        assert "my-secret-tok" in r.text

    def test_step_navigation(self, client):
        # Step 1 → Step 2
        r = client.post(
            "/setup/step/2",
            data={
                "paperless_url": "http://p:8000",
                "paperless_token": "tok",
                "paperless_inbox_tag_id": "5",
            },
        )
        assert r.status_code == 200
        assert "Ollama" in r.text

    def test_step_back_navigation(self, client):
        # Step 2 → Step 1
        r = client.post(
            "/setup/step/1",
            data={"paperless_url": "http://p:8000", "ollama_url": "http://o:11434"},
        )
        assert r.status_code == 200
        assert "Paperless" in r.text

    def test_step_to_summary(self, client):
        # Step 3 → Step 4 (summary)
        r = client.post(
            "/setup/step/4",
            data={
                "paperless_url": "http://p:8000",
                "paperless_token": "tok",
                "paperless_inbox_tag_id": "5",
                "ollama_url": "http://o:11434",
                "ollama_model": "gemma4:e2b",
            },
        )
        assert r.status_code == 200
        assert "Review Configuration" in r.text
        assert "http://p:8000" in r.text

    def test_telegram_step(self, client):
        r = client.post("/setup/step/3", data={})
        assert r.status_code == 200
        assert "Telegram" in r.text


# ---------------------------------------------------------------------------
# Connection tests
# ---------------------------------------------------------------------------
class TestConnectionTests:
    def test_paperless_test_missing_fields(self, client):
        r = client.post("/setup/test-paperless", data={"paperless_url": "", "paperless_token": ""})
        assert r.status_code == 200
        assert "required" in r.text.lower()

    def test_paperless_test_success(self, client):
        mock_client = AsyncMock()
        mock_client.ping = AsyncMock(return_value=True)
        mock_client.list_tags = AsyncMock(
            return_value=[
                PaperlessEntity(id=1, name="Posteingang"),
                PaperlessEntity(id=2, name="Archiv"),
            ]
        )
        mock_client.aclose = AsyncMock()

        with patch("app.clients.paperless.PaperlessClient", return_value=mock_client):
            r = client.post(
                "/setup/test-paperless",
                data={"paperless_url": "http://p:8000", "paperless_token": "tok"},
            )
        assert r.status_code == 200
        assert "Connected successfully" in r.text
        assert "Posteingang" in r.text
        assert "Archiv" in r.text
        assert "<select" in r.text
        assert 'name="paperless_inbox_tag_id"' in r.text

    def test_paperless_test_failure(self, client):
        mock_client = AsyncMock()
        mock_client.ping = AsyncMock(return_value=False)
        mock_client.aclose = AsyncMock()

        with patch("app.clients.paperless.PaperlessClient", return_value=mock_client):
            r = client.post(
                "/setup/test-paperless",
                data={"paperless_url": "http://bad:8000", "paperless_token": "tok"},
            )
        assert r.status_code == 200
        assert "failed" in r.text.lower()

    def test_ollama_test_missing_url(self, client):
        r = client.post("/setup/test-ollama", data={"ollama_url": "", "ollama_model": ""})
        assert r.status_code == 200
        assert "required" in r.text.lower()

    def test_ollama_test_success(self, client):
        from unittest.mock import MagicMock

        mock_client = AsyncMock()
        mock_client.ping = AsyncMock(return_value=True)
        # _client.get() is called with await, so it must be AsyncMock,
        # but the returned response needs sync .json() and .raise_for_status()
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = {
            "models": [{"name": "gemma4:e2b"}, {"name": "llama3:8b"}]
        }
        mock_response.raise_for_status = MagicMock()
        mock_http_client = AsyncMock()
        mock_http_client.get = AsyncMock(return_value=mock_response)
        mock_client._client = mock_http_client
        mock_client.aclose = AsyncMock()

        with patch("app.clients.ollama.OllamaClient", return_value=mock_client):
            r = client.post(
                "/setup/test-ollama",
                data={"ollama_url": "http://o:11434", "ollama_model": "gemma4:e2b"},
            )
        assert r.status_code == 200
        assert "Connected" in r.text
        assert "gemma4:e2b" in r.text

    def test_telegram_test_missing_fields(self, client):
        r = client.post(
            "/setup/test-telegram",
            data={"telegram_bot_token": "", "telegram_chat_id": ""},
        )
        assert r.status_code == 200
        assert "required" in r.text.lower()


# ---------------------------------------------------------------------------
# Complete setup
# ---------------------------------------------------------------------------
class TestCompleteSetup:
    def test_complete_missing_paperless(self, client):
        r = client.post(
            "/setup/complete",
            data={"ollama_url": "http://o:11434"},
            follow_redirects=False,
        )
        assert r.status_code == 400

    def test_complete_missing_inbox_tag(self, client):
        r = client.post(
            "/setup/complete",
            data={
                "paperless_url": "http://p:8000",
                "paperless_token": "tok",
                "paperless_inbox_tag_id": "0",
            },
            follow_redirects=False,
        )
        assert r.status_code == 400

    def test_complete_success(self, client, monkeypatch):
        # Mock client constructors and dependencies
        mock_paperless = AsyncMock()
        mock_ollama = AsyncMock()
        mock_telegram = AsyncMock()
        mock_telegram.enabled = False

        with (
            patch(
                "app.config_writer.save_config",
                return_value=({"paperless_url": "http://p:8000"}, set()),
            ),
            patch("app.clients.paperless.PaperlessClient", return_value=mock_paperless),
            patch("app.clients.ollama.OllamaClient", return_value=mock_ollama),
            patch("app.clients.telegram.TelegramClient", return_value=mock_telegram),
            patch("app.worker.start_scheduler"),
            patch("app.telegram_handler.start_telegram"),
        ):
            r = client.post(
                "/setup/complete",
                data={
                    "paperless_url": "http://p:8000",
                    "paperless_token": "tok",
                    "paperless_inbox_tag_id": "5",
                    "ollama_url": "http://o:11434",
                    "ollama_model": "gemma4:e2b",
                },
                follow_redirects=False,
            )
        assert r.status_code == 200
        assert r.headers.get("hx-redirect") == "/"


# ---------------------------------------------------------------------------
# Settings save-config
# ---------------------------------------------------------------------------
class TestSettingsSave:
    def test_settings_page_renders_grouped(self, client):
        r = client.get("/settings")
        assert r.status_code == 200
        assert "Save Configuration" in r.text
        assert "Paperless" in r.text
        assert "Ollama" in r.text

    def test_save_config_no_changes(self, client):
        from app.config import settings

        with patch("app.config_writer.save_config", return_value=({}, set())):
            r = client.post(
                "/settings/save-config",
                data={"max_doc_chars": str(settings.max_doc_chars)},
            )
        assert r.status_code == 200
        assert "No changes" in r.text

    def test_save_config_with_changes(self, client):
        with (
            patch("app.config_writer.save_config", return_value=({"max_doc_chars": 5000}, set())),
            patch(
                "app.config_writer.apply_runtime_changes", new_callable=AsyncMock, return_value=[]
            ),
        ):
            r = client.post(
                "/settings/save-config",
                data={"max_doc_chars": "5000"},
            )
        assert r.status_code == 200
        assert "Saved" in r.text
        assert "max_doc_chars" in r.text

    def test_save_config_restart_required(self, client):
        with (
            patch("app.config_writer.save_config", return_value=({"gui_port": 9999}, {"gui_port"})),
            patch(
                "app.config_writer.apply_runtime_changes", new_callable=AsyncMock, return_value=[]
            ),
        ):
            r = client.post(
                "/settings/save-config",
                data={"gui_port": "9999"},
            )
        assert r.status_code == 200
        assert "Restart" in r.text or "restart" in r.text


# ---------------------------------------------------------------------------
# Setup redirect middleware
# ---------------------------------------------------------------------------
class TestSetupRedirect:
    def test_redirect_when_needs_setup(self, client, monkeypatch):
        monkeypatch.setattr("app.main.needs_setup", lambda: True)
        r = client.get("/", follow_redirects=False)
        assert r.status_code == 302
        assert "/setup" in r.headers["location"]

    def test_no_redirect_when_configured(self, client, monkeypatch):
        monkeypatch.setattr("app.main.needs_setup", lambda: False)
        r = client.get("/")
        assert r.status_code == 200

    def test_setup_not_redirected(self, client, monkeypatch):
        monkeypatch.setattr("app.main.needs_setup", lambda: True)
        r = client.get("/setup")
        assert r.status_code == 200

    def test_healthz_not_redirected(self, client, monkeypatch):
        monkeypatch.setattr("app.main.needs_setup", lambda: True)
        r = client.get("/healthz")
        assert r.status_code == 200

    def test_static_not_redirected(self, client, monkeypatch):
        monkeypatch.setattr("app.main.needs_setup", lambda: True)
        r = client.get("/static/app.css")
        # May be 200 or 404 depending on file existence, but NOT 302
        assert r.status_code != 302
