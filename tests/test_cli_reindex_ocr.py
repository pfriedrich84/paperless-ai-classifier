"""Tests for the CLI reindex-ocr --force flag."""

from __future__ import annotations

import asyncio
import sys
from unittest.mock import AsyncMock, MagicMock, patch

import pytest


@pytest.fixture(autouse=True)
def _mock_cli_side_effects(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("app.cli.init_db", MagicMock())
    monkeypatch.setattr("app.cli._configure_logging", MagicMock())


def test_cmd_reindex_ocr_passes_force() -> None:
    """cmd_reindex_ocr(force=True) passes force through to batch_correct_documents."""
    mock_batch = AsyncMock(return_value=5)
    mock_paperless = MagicMock()
    mock_paperless.aclose = AsyncMock()
    mock_ollama = MagicMock()
    mock_ollama.aclose = AsyncMock()

    with (
        patch("app.cli.PaperlessClient", return_value=mock_paperless),
        patch("app.cli.OllamaClient", return_value=mock_ollama),
        patch("app.pipeline.ocr_correction.batch_correct_documents", mock_batch),
        patch("app.pipeline.ocr_correction.effective_ocr_mode", return_value="text"),
    ):
        from app.cli import cmd_reindex_ocr

        asyncio.run(cmd_reindex_ocr(force=True))

    mock_batch.assert_called_once_with(mock_paperless, mock_ollama, force=True)


def test_cmd_reindex_ocr_default_no_force() -> None:
    """cmd_reindex_ocr() defaults to force=False."""
    mock_batch = AsyncMock(return_value=0)
    mock_paperless = MagicMock()
    mock_paperless.aclose = AsyncMock()
    mock_ollama = MagicMock()
    mock_ollama.aclose = AsyncMock()

    with (
        patch("app.cli.PaperlessClient", return_value=mock_paperless),
        patch("app.cli.OllamaClient", return_value=mock_ollama),
        patch("app.pipeline.ocr_correction.batch_correct_documents", mock_batch),
        patch("app.pipeline.ocr_correction.effective_ocr_mode", return_value="text"),
    ):
        from app.cli import cmd_reindex_ocr

        asyncio.run(cmd_reindex_ocr())

    mock_batch.assert_called_once_with(mock_paperless, mock_ollama, force=False)


def test_main_parses_force_flag(monkeypatch: pytest.MonkeyPatch) -> None:
    """main() parses --force and passes it to cmd_reindex_ocr."""
    monkeypatch.setattr(sys, "argv", ["cli", "reindex-ocr", "--force"])

    mock_cmd = AsyncMock()

    with patch("app.cli.COMMANDS", {"reindex-ocr": ("desc", mock_cmd)}):
        from app.cli import main

        main()

    mock_cmd.assert_called_once_with(force=True)


def test_main_no_force_flag(monkeypatch: pytest.MonkeyPatch) -> None:
    """main() passes force=False when --force is not given."""
    monkeypatch.setattr(sys, "argv", ["cli", "reindex-ocr"])

    mock_cmd = AsyncMock()

    with patch("app.cli.COMMANDS", {"reindex-ocr": ("desc", mock_cmd)}):
        from app.cli import main

        main()

    mock_cmd.assert_called_once_with(force=False)
