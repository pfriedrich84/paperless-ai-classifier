from __future__ import annotations

import importlib

from starlette.testclient import TestClient

import app.main as app_main
from app.config import settings


def test_cors_origin_list_parsing():
    original = settings.cors_allowed_origins
    try:
        object.__setattr__(settings, "cors_allowed_origins", " https://a.test, ,https://b.test ")
        assert settings.cors_allowed_origins_list == ["https://a.test", "https://b.test"]
    finally:
        object.__setattr__(settings, "cors_allowed_origins", original)


def test_cors_disabled_by_default():
    client = TestClient(app_main.app)
    response = client.get("/healthz", headers={"Origin": "https://frontend.example"})

    assert "access-control-allow-origin" not in response.headers


def test_cors_allows_configured_origin():
    original = settings.cors_allowed_origins
    try:
        object.__setattr__(settings, "cors_allowed_origins", "https://frontend.example")
        reloaded = importlib.reload(app_main)
        client = TestClient(reloaded.app)

        response = client.options(
            "/healthz",
            headers={
                "Origin": "https://frontend.example",
                "Access-Control-Request-Method": "GET",
            },
        )

        assert response.status_code == 200
        assert response.headers["access-control-allow-origin"] == "https://frontend.example"
    finally:
        object.__setattr__(settings, "cors_allowed_origins", original)
        importlib.reload(app_main)
