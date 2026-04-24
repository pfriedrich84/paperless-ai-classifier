from __future__ import annotations

from pathlib import Path


def test_prompts_dir_prefers_source_tree():
    from app.config import Settings

    cfg = Settings()

    prompts_dir = cfg.prompts_dir

    assert prompts_dir.name == "prompts"
    assert prompts_dir.is_dir()
    assert (prompts_dir / "classify_system.txt").is_file()


def test_prompts_dir_falls_back_to_workdir_prompts(tmp_path, monkeypatch):
    from app.config import Settings

    workdir_prompts = tmp_path / "prompts"
    workdir_prompts.mkdir()
    (workdir_prompts / "classify_system.txt").write_text("fallback", encoding="utf-8")
    monkeypatch.chdir(tmp_path)

    import app.config as config_module

    source_prompts = Path(config_module.__file__).parent.parent / "prompts"
    original_is_dir = Path.is_dir

    def fake_is_dir(self: Path) -> bool:
        if self == source_prompts:
            return False
        return original_is_dir(self)

    monkeypatch.setattr(Path, "is_dir", fake_is_dir)

    cfg = Settings()

    assert cfg.prompts_dir == workdir_prompts
