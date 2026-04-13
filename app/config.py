"""Application configuration via pydantic-settings (.env-driven)."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """All runtime settings. Everything is driven from environment variables."""

    model_config = SettingsConfigDict(
        env_file=("/data/config.env", ".env"),
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # --- Paperless ---
    paperless_url: str = ""
    paperless_token: str = ""
    paperless_inbox_tag_id: int = 0
    paperless_processed_tag_id: int | None = None
    keep_inbox_tag: bool = True

    # --- Ollama ---
    ollama_url: str = "http://ollama:11434"
    ollama_model: str = "gemma4:e2b"
    ollama_embed_model: str = "nomic-embed-text-v2-moe"
    ollama_ocr_model: str = "gemma3:1b"
    ollama_timeout_seconds: int = 300
    ollama_embed_retries: int = 3
    ollama_embed_retry_base_delay: float = 1.0
    ollama_num_ctx: int = 8192

    # --- Worker ---
    poll_interval_seconds: int = 300
    context_max_docs: int = 5
    max_doc_chars: int = 8000
    auto_commit_confidence: int = 0  # 0 = immer manuell reviewen
    enable_ocr_correction: bool = False

    # --- GUI ---
    gui_port: int = 8088
    gui_base_url: str = ""  # e.g. "https://classifier.local:8088" for Telegram links
    gui_username: str = ""
    gui_password: str = ""

    # --- Telegram ---
    enable_telegram: bool = False
    telegram_bot_token: str = ""
    telegram_chat_id: str = ""
    telegram_poll_interval: int = 5  # seconds between getUpdates calls

    # --- Webhook ---
    webhook_secret: str = ""  # if set, POST /webhook/paperless requires this token

    # --- MCP ---
    mcp_transport: str = "stdio"  # stdio | sse | streamable-http
    mcp_port: int = 3001
    mcp_host: str = "0.0.0.0"
    mcp_enable_write: bool = False  # write tools only registered when True
    mcp_api_key: str = ""  # empty = no auth (ok for stdio)
    mcp_classify_rate_limit: int = 10  # max classifications per hour, 0 = unlimited

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


def needs_setup() -> bool:
    """True when essential Paperless fields are missing (first-run state)."""
    return (
        not settings.paperless_url
        or not settings.paperless_token
        or settings.paperless_inbox_tag_id == 0
    )


# ---------------------------------------------------------------------------
# Field metadata for UI rendering
# ---------------------------------------------------------------------------
class _FieldMeta(dict):
    """Typed dict for field metadata — just a plain dict with documentation."""


def _fm(
    category: str,
    label: str,
    input_type: str = "text",
    *,
    required: bool = False,
    restart: str | None = None,
    help: str = "",
    sensitive: bool = False,
) -> dict[str, Any]:
    return {
        "category": category,
        "label": label,
        "input_type": input_type,
        "required": required,
        "restart": restart,
        "help": help,
        "sensitive": sensitive,
    }


FIELD_META: dict[str, dict[str, Any]] = {
    # --- Paperless ---
    "paperless_url": _fm(
        "Paperless",
        "Paperless URL",
        "url",
        required=True,
        restart="component",
        help="Base URL of your Paperless-NGX instance",
    ),
    "paperless_token": _fm(
        "Paperless",
        "API Token",
        "password",
        required=True,
        restart="component",
        help="Paperless API authentication token",
        sensitive=True,
    ),
    "paperless_inbox_tag_id": _fm(
        "Paperless",
        "Inbox Tag ID",
        "number",
        required=True,
        restart="component",
        help="Tag ID used as inbox (e.g. 'Posteingang')",
    ),
    "paperless_processed_tag_id": _fm(
        "Paperless",
        "Processed Tag ID",
        "number",
        help="Optional tag ID added after commit (e.g. 'Verarbeitet')",
    ),
    "keep_inbox_tag": _fm(
        "Paperless", "Keep Inbox Tag", "bool", help="Keep the inbox tag on documents after commit"
    ),
    # --- Ollama ---
    "ollama_url": _fm(
        "Ollama", "Ollama URL", "url", restart="component", help="Base URL of the Ollama server"
    ),
    "ollama_model": _fm(
        "Ollama", "Chat Model", restart="component", help="Ollama model for classification"
    ),
    "ollama_embed_model": _fm(
        "Ollama", "Embedding Model", restart="component", help="Ollama model for embeddings"
    ),
    "ollama_ocr_model": _fm(
        "Ollama",
        "OCR Model",
        restart="component",
        help="Smaller model for OCR correction (only used when OCR correction is enabled)",
    ),
    "ollama_timeout_seconds": _fm(
        "Ollama",
        "Timeout (seconds)",
        "number",
        restart="component",
        help="HTTP timeout for Ollama requests",
    ),
    "ollama_embed_retries": _fm(
        "Ollama", "Embed Retries", "number", help="Max retries for embedding requests"
    ),
    "ollama_embed_retry_base_delay": _fm(
        "Ollama", "Embed Retry Delay", "number", help="Base delay (seconds) for embed retry backoff"
    ),
    "ollama_num_ctx": _fm(
        "Ollama", "Context Window (tokens)", "number", help="num_ctx for the chat model"
    ),
    # --- Worker ---
    "poll_interval_seconds": _fm(
        "Worker",
        "Poll Interval (seconds)",
        "number",
        restart="component",
        help="Seconds between inbox polls",
    ),
    "context_max_docs": _fm(
        "Worker", "Context Max Docs", "number", help="Max similar documents used as context"
    ),
    "max_doc_chars": _fm(
        "Worker",
        "Max Document Chars",
        "number",
        help="Max characters of document text sent to the LLM",
    ),
    "auto_commit_confidence": _fm(
        "Worker",
        "Auto-Commit Confidence",
        "number",
        help="0 = always review. Set to e.g. 85 to auto-commit high-confidence results",
    ),
    "enable_ocr_correction": _fm(
        "Worker",
        "Enable OCR Correction",
        "bool",
        help="Run LLM-based OCR correction before classification",
    ),
    # --- GUI ---
    "gui_port": _fm("GUI", "Port", "number", restart="app", help="Web UI port (requires restart)"),
    "gui_base_url": _fm(
        "GUI",
        "External Base URL",
        "url",
        help="External URL for Telegram links (e.g. https://classifier.local:8088)",
    ),
    "gui_username": _fm(
        "GUI", "Basic Auth Username", restart="app", help="Leave empty to disable Basic Auth"
    ),
    "gui_password": _fm(
        "GUI",
        "Basic Auth Password",
        "password",
        restart="app",
        help="Basic Auth password",
        sensitive=True,
    ),
    # --- Telegram ---
    "enable_telegram": _fm(
        "Telegram",
        "Enable Telegram",
        "bool",
        restart="component",
        help="Enable Telegram notifications and inline approval",
    ),
    "telegram_bot_token": _fm(
        "Telegram",
        "Bot Token",
        "password",
        restart="component",
        help="Telegram Bot API token from @BotFather",
        sensitive=True,
    ),
    "telegram_chat_id": _fm(
        "Telegram", "Chat ID", restart="component", help="Telegram chat/group ID for notifications"
    ),
    "telegram_poll_interval": _fm(
        "Telegram",
        "Poll Interval (seconds)",
        "number",
        help="Seconds between Telegram getUpdates calls",
    ),
    # --- Webhook ---
    "webhook_secret": _fm(
        "Webhook",
        "Webhook Secret",
        "password",
        help="Shared secret for POST /webhook/paperless",
        sensitive=True,
    ),
    # --- MCP ---
    "mcp_transport": _fm("MCP", "Transport", restart="app", help="stdio | sse | streamable-http"),
    "mcp_port": _fm("MCP", "Port", "number", restart="app", help="MCP server port (SSE/HTTP)"),
    "mcp_host": _fm("MCP", "Host", restart="app", help="MCP server bind address"),
    "mcp_enable_write": _fm(
        "MCP", "Enable Write Tools", "bool", restart="app", help="Allow write operations via MCP"
    ),
    "mcp_api_key": _fm(
        "MCP",
        "API Key",
        "password",
        restart="app",
        help="API key for MCP auth (recommended for SSE)",
        sensitive=True,
    ),
    "mcp_classify_rate_limit": _fm(
        "MCP",
        "Classify Rate Limit",
        "number",
        restart="app",
        help="Max AI classifications per hour (0 = unlimited)",
    ),
    # --- System ---
    "data_dir": _fm(
        "System",
        "Data Directory",
        restart="app",
        help="Persistent data directory (DB, config, prompts)",
    ),
    "log_level": _fm("System", "Log Level", restart="app", help="DEBUG, INFO, WARNING, ERROR"),
}
