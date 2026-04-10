"""Application configuration via pydantic-settings (.env-driven)."""

from __future__ import annotations

from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """All runtime settings. Everything is driven from environment variables."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # --- Paperless ---
    paperless_url: str
    paperless_token: str
    paperless_inbox_tag_id: int
    paperless_processed_tag_id: int | None = None

    # --- Ollama ---
    ollama_url: str = "http://ollama:11434"
    ollama_model: str = "gemma3:4b"
    ollama_embed_model: str = "nomic-embed-text"
    ollama_timeout_seconds: int = 300

    # --- Worker ---
    poll_interval_seconds: int = 300
    context_max_docs: int = 5
    max_doc_chars: int = 8000
    auto_commit_confidence: int = 0  # 0 = immer manuell reviewen
    enable_ocr_correction: bool = False

    # --- GUI ---
    gui_port: int = 8088
    gui_username: str = ""
    gui_password: str = ""

    # --- Telegram ---
    enable_telegram: bool = False
    telegram_bot_token: str = ""
    telegram_chat_id: str = ""
    telegram_poll_interval: int = 5  # seconds between getUpdates calls

    # --- State ---
    data_dir: str = "/data"
    log_level: str = "INFO"

    @property
    def db_path(self) -> Path:
        return Path(self.data_dir) / "classifier.db"

    @property
    def prompts_dir(self) -> Path:
        return Path(__file__).parent.parent / "prompts"


# Singleton
settings = Settings()  # type: ignore[call-arg]
