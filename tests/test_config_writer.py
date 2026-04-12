"""Tests for config_writer — env file I/O and save_config logic."""

from __future__ import annotations

from collections import OrderedDict
from pathlib import Path

import pytest


# ---------------------------------------------------------------------------
# read_env_file / write_env_file
# ---------------------------------------------------------------------------
class TestEnvFileIO:
    def test_read_empty_file(self, tmp_path: Path):
        from app.config_writer import read_env_file

        p = tmp_path / "config.env"
        p.write_text("", encoding="utf-8")
        assert read_env_file(p) == OrderedDict()

    def test_read_missing_file(self, tmp_path: Path):
        from app.config_writer import read_env_file

        p = tmp_path / "nonexistent.env"
        assert read_env_file(p) == OrderedDict()

    def test_read_key_value_pairs(self, tmp_path: Path):
        from app.config_writer import read_env_file

        p = tmp_path / "config.env"
        p.write_text(
            "# comment\nFOO=bar\nBAZ=123\n\n# another comment\nEMPTY=\n",
            encoding="utf-8",
        )
        result = read_env_file(p)
        assert result == OrderedDict([("FOO", "bar"), ("BAZ", "123"), ("EMPTY", "")])

    def test_write_creates_file(self, tmp_path: Path):
        from app.config_writer import write_env_file

        p = tmp_path / "new_config.env"
        values = OrderedDict([("A", "1"), ("B", "hello")])
        write_env_file(p, values)
        assert p.is_file()
        content = p.read_text(encoding="utf-8")
        assert "A=1\n" in content
        assert "B=hello\n" in content

    def test_write_creates_backup(self, tmp_path: Path):
        from app.config_writer import write_env_file

        p = tmp_path / "config.env"
        p.write_text("OLD=value\n", encoding="utf-8")
        write_env_file(p, OrderedDict([("NEW", "value")]))
        backups = list(tmp_path.glob("config.bak.*"))
        assert len(backups) == 1
        assert "OLD=value" in backups[0].read_text(encoding="utf-8")

    def test_write_atomic_no_tmp_leftover(self, tmp_path: Path):
        from app.config_writer import write_env_file

        p = tmp_path / "config.env"
        write_env_file(p, OrderedDict([("X", "y")]))
        tmps = list(tmp_path.glob("*.tmp"))
        assert len(tmps) == 0

    def test_write_creates_parent_dirs(self, tmp_path: Path):
        from app.config_writer import write_env_file

        p = tmp_path / "sub" / "dir" / "config.env"
        write_env_file(p, OrderedDict([("K", "V")]))
        assert p.is_file()


# ---------------------------------------------------------------------------
# save_config
# ---------------------------------------------------------------------------
class TestSaveConfig:
    def test_save_updates_in_memory_and_file(self, tmp_path: Path, monkeypatch):
        from app.config import settings
        from app.config_writer import save_config

        monkeypatch.setattr("app.config.settings.data_dir", str(tmp_path))

        original_max = settings.max_doc_chars
        changed, restart = save_config({"max_doc_chars": 5000})

        assert "max_doc_chars" in changed
        assert settings.max_doc_chars == 5000
        assert not restart  # max_doc_chars has no restart requirement

        # Restore
        object.__setattr__(settings, "max_doc_chars", original_max)

    def test_save_detects_no_change(self, tmp_path: Path, monkeypatch):
        from app.config import settings
        from app.config_writer import save_config

        monkeypatch.setattr("app.config.settings.data_dir", str(tmp_path))

        # Write initial value
        save_config({"max_doc_chars": settings.max_doc_chars})
        # Save same value again
        changed, _ = save_config({"max_doc_chars": settings.max_doc_chars})
        assert not changed

    def test_save_bool_field(self, tmp_path: Path, monkeypatch):
        from app.config import settings
        from app.config_writer import save_config

        monkeypatch.setattr("app.config.settings.data_dir", str(tmp_path))

        original = settings.keep_inbox_tag
        save_config({"keep_inbox_tag": "false"})
        assert settings.keep_inbox_tag is False

        # Restore
        object.__setattr__(settings, "keep_inbox_tag", original)

    def test_save_ignores_unknown_fields(self, tmp_path: Path, monkeypatch):
        from app.config_writer import save_config

        monkeypatch.setattr("app.config.settings.data_dir", str(tmp_path))

        changed, _ = save_config({"totally_fake_field": "value"})
        assert not changed

    def test_save_reports_restart_required(self, tmp_path: Path, monkeypatch):
        from app.config import settings
        from app.config_writer import save_config

        monkeypatch.setattr("app.config.settings.data_dir", str(tmp_path))

        original = settings.gui_port
        changed, restart = save_config({"gui_port": 9999})
        assert "gui_port" in restart
        assert "gui_port" in changed

        # Restore
        object.__setattr__(settings, "gui_port", original)

    def test_save_int_validation(self, tmp_path: Path, monkeypatch):
        from app.config_writer import save_config

        monkeypatch.setattr("app.config.settings.data_dir", str(tmp_path))

        # Non-numeric string for int field should be skipped
        changed, _ = save_config({"gui_port": "not_a_number"})
        assert "gui_port" not in changed
