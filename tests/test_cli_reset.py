"""Tests for the CLI reset command."""

from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from app.cli import cmd_reset


@pytest.fixture()
def data_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Set DATA_DIR to a temp directory and mock init_db."""
    monkeypatch.setattr("app.config.settings.data_dir", str(tmp_path))
    monkeypatch.setattr("app.cli.init_db", MagicMock())
    return tmp_path


def _create_db_files(data_dir: Path) -> tuple[Path, Path, Path]:
    """Create fake DB + WAL/SHM files and return their paths."""
    db = data_dir / "classifier.db"
    wal = data_dir / "classifier.db-wal"
    shm = data_dir / "classifier.db-shm"
    db.write_text("fake db")
    wal.write_text("wal")
    shm.write_text("shm")
    return db, wal, shm


def test_reset_deletes_db_and_recreates(data_dir: Path) -> None:
    """DB + WAL/SHM files are deleted and init_db() is called."""
    db, wal, shm = _create_db_files(data_dir)

    cmd_reset(include_config=False)

    assert not db.exists()
    assert not wal.exists()
    assert not shm.exists()

    from app.cli import init_db as patched_init_db

    patched_init_db.assert_called_once()


def test_reset_include_config(data_dir: Path) -> None:
    """config.env and backup files are deleted when --include-config is set."""
    _create_db_files(data_dir)
    config_env = data_dir / "config.env"
    config_env.write_text("OLLAMA_URL=http://test")
    bak1 = data_dir / "config.bak.20240101120000"
    bak1.write_text("old")
    bak2 = data_dir / "config.bak.20240315090000"
    bak2.write_text("older")

    cmd_reset(include_config=True)

    assert not config_env.exists()
    assert not bak1.exists()
    assert not bak2.exists()


def test_reset_without_include_config_keeps_env(data_dir: Path) -> None:
    """config.env is preserved when --include-config is not set."""
    _create_db_files(data_dir)
    config_env = data_dir / "config.env"
    config_env.write_text("OLLAMA_URL=http://test")

    cmd_reset(include_config=False)

    assert config_env.exists()


def test_reset_idempotent(data_dir: Path) -> None:
    """No error when no state files exist; init_db() is still called."""
    cmd_reset(include_config=False)

    from app.cli import init_db as patched_init_db

    patched_init_db.assert_called_once()


def test_reset_requires_yes_flag(monkeypatch: pytest.MonkeyPatch) -> None:
    """main() exits with error when --yes is missing."""
    monkeypatch.setattr(sys, "argv", ["cli", "reset"])
    monkeypatch.setattr("app.cli._configure_logging", MagicMock())

    from app.cli import main

    with pytest.raises(SystemExit, match="1"):
        main()
