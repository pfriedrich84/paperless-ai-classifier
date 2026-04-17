"""Application configuration via pydantic-settings (.env-driven)."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """All runtime settings. Everything is driven from environment variables."""

    model_config = SettingsConfigDict(
        env_file=".env",
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
    ollama_model: str = "gemma4:e4b"
    ollama_embed_model: str = "qwen3-embedding:0.6b"
    ollama_ocr_model: str = "qwen3:0.6b"
    ollama_timeout_seconds: int = 300
    ollama_embed_retries: int = 3
    ollama_embed_retry_base_delay: float = 1.0
    ollama_chat_retries: int = 1
    ollama_chat_retry_base_delay: float = 1.0
    ollama_num_ctx: int = 16384
    ollama_embed_num_ctx: int = 8192
    ollama_ocr_num_ctx: int = 16384
    ollama_model_swap_delay: float = 5.0  # seconds to wait after unloading a model

    # --- OCR ---
    ocr_mode: str = "off"  # off | text | vision_light | vision_full
    ocr_vision_model: str = ""  # empty = use ollama_model (must be vision-capable)
    ocr_vision_max_pages: int = 3
    ocr_vision_dpi: int = 150

    # --- Worker ---
    poll_interval_seconds: int = 0
    context_max_docs: int = 5
    context_max_distance: float = 0.0  # 0 = no threshold; e.g. 1.5 filters irrelevant docs
    hybrid_search_weight: float = 0.7  # 0.0 = FTS only, 1.0 = vector only, 0.7 = default blend
    max_doc_chars: int = 24000
    embed_max_chars: int = 6000
    auto_commit_confidence: int = 0  # 0 = immer manuell reviewen
    enable_ocr_correction: bool = False  # deprecated, use ocr_mode instead

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
        # Source-relative (works in development)
        p = Path(__file__).parent.parent / "prompts"
        if p.is_dir():
            return p
        # Installed package: fall back to working directory (Docker WORKDIR)
        return Path.cwd() / "prompts"


# Singleton
settings = Settings()  # type: ignore[call-arg]


def _apply_config_env_overrides() -> None:
    """Apply config.env overrides with highest priority.

    Docker-compose injects .env values as OS environment variables, which
    pydantic-settings prioritises over dotenv files.  This means changes
    saved via the Settings UI (written to config.env) are lost on restart.

    Fix: read config.env *after* the singleton is created and patch the
    values in, so they effectively have the highest priority.
    """
    config_path = Path(settings.data_dir) / "config.env"
    if not config_path.is_file():
        return

    for line in config_path.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            continue
        key, _, raw = stripped.partition("=")
        field_name = key.strip().lower()
        raw = raw.strip()

        if field_name not in Settings.model_fields:
            continue

        default = Settings.model_fields[field_name].default
        try:
            if isinstance(default, bool):
                coerced: Any = raw.lower() in ("true", "1", "yes")
            elif isinstance(default, int):
                coerced = int(raw)
            elif isinstance(default, float):
                coerced = float(raw)
            elif default is None:
                # Optional field (e.g. int | None)
                coerced = None if not raw or raw.lower() == "none" else int(raw)
            else:
                coerced = raw
        except (ValueError, TypeError):
            continue

        object.__setattr__(settings, field_name, coerced)


_apply_config_env_overrides()


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
    # --- Ollama (shared) ---
    "ollama_url": _fm(
        "Ollama", "Ollama URL", "url", restart="component", help="Base URL of the Ollama server"
    ),
    "ollama_timeout_seconds": _fm(
        "Ollama",
        "Timeout (seconds)",
        "number",
        restart="component",
        help="HTTP timeout for Ollama requests",
    ),
    "ollama_model_swap_delay": _fm(
        "Ollama",
        "Model Swap Delay (seconds)",
        "number",
        help="Seconds to wait after unloading a model before loading the next one. "
        "Gives the GPU time to free memory so Ollama detects correct VRAM. "
        "Set to 0 to disable.",
    ),
    # --- Phase 1: OCR ---
    "ocr_mode": _fm(
        "Phase 1: OCR",
        "OCR Mode",
        help="off | text | vision_light | vision_full",
    ),
    "ollama_ocr_model": _fm(
        "Phase 1: OCR",
        "OCR Text Model",
        restart="component",
        help="Smaller model for text-only OCR correction (ocr_mode=text)",
    ),
    "ocr_vision_model": _fm(
        "Phase 1: OCR",
        "Vision Model",
        restart="component",
        help="Ollama model for vision OCR (empty = use Classification Model)",
    ),
    "ocr_vision_max_pages": _fm(
        "Phase 1: OCR",
        "Vision Max Pages",
        "number",
        help="Max document pages to process with vision model",
    ),
    "ocr_vision_dpi": _fm(
        "Phase 1: OCR",
        "Vision DPI",
        "number",
        help="Render resolution for PDF pages (pixels per inch)",
    ),
    "ollama_ocr_num_ctx": _fm(
        "Phase 1: OCR",
        "OCR Context Window (tokens)",
        "number",
        help="num_ctx for OCR models. Vision OCR needs more context (~1536 tokens/page image). Default: 16384.",
    ),
    "enable_ocr_correction": _fm(
        "Phase 1: OCR",
        "Enable OCR Correction (deprecated)",
        "bool",
        help="Deprecated: use OCR_MODE=text instead. Kept for backwards compatibility.",
    ),
    # --- Phase 2: Embedding ---
    "ollama_embed_model": _fm(
        "Phase 2: Embedding",
        "Embedding Model",
        restart="component",
        help="Ollama model for embeddings (e.g. qwen3-embedding:0.6b)",
    ),
    "ollama_embed_num_ctx": _fm(
        "Phase 2: Embedding",
        "Context Window (tokens)",
        "number",
        help="num_ctx for the embedding model (Ollama may clamp to model's n_ctx_train — check Ollama logs)",
    ),
    "embed_max_chars": _fm(
        "Phase 2: Embedding",
        "Max Document Chars",
        "number",
        help="Max characters of document text used for embedding (similarity search)",
    ),
    "ollama_embed_retries": _fm(
        "Phase 2: Embedding",
        "Retries",
        "number",
        help="Max retries for embedding requests (context-length + transient errors)",
    ),
    "ollama_embed_retry_base_delay": _fm(
        "Phase 2: Embedding",
        "Retry Base Delay (seconds)",
        "number",
        help="Base delay for exponential backoff on transient errors",
    ),
    # --- Phase 3: Klassifikation ---
    "ollama_model": _fm(
        "Phase 3: Klassifikation",
        "Classification Model",
        restart="component",
        help="Ollama model for classification (e.g. gemma4:e4b)",
    ),
    "ollama_num_ctx": _fm(
        "Phase 3: Klassifikation",
        "Context Window (tokens)",
        "number",
        help="num_ctx for the classification model",
    ),
    "ollama_chat_retries": _fm(
        "Phase 3: Klassifikation",
        "Retries",
        "number",
        help="Max retries for chat/classification/OCR requests on transient errors",
    ),
    "ollama_chat_retry_base_delay": _fm(
        "Phase 3: Klassifikation",
        "Retry Base Delay (seconds)",
        "number",
        help="Base delay for exponential backoff on transient chat errors",
    ),
    "max_doc_chars": _fm(
        "Phase 3: Klassifikation",
        "Max Document Chars",
        "number",
        help="Max characters of document text sent to the classification LLM",
    ),
    "context_max_docs": _fm(
        "Phase 3: Klassifikation",
        "Context Max Docs",
        "number",
        help="Max similar documents used as few-shot context",
    ),
    "context_max_distance": _fm(
        "Phase 3: Klassifikation",
        "Context Max Distance",
        "number",
        help="Max L2 distance for context docs (0 = no threshold). Lower values = stricter relevance filtering.",
    ),
    "hybrid_search_weight": _fm(
        "Phase 2: Embedding",
        "Hybrid Search Weight",
        "number",
        help="Blend ratio for hybrid search: 0.0 = keyword only, 1.0 = vector only, 0.7 = default",
    ),
    "auto_commit_confidence": _fm(
        "Phase 3: Klassifikation",
        "Auto-Commit Confidence",
        "number",
        help="0 = always review. Set to e.g. 85 to auto-commit high-confidence results",
    ),
    # --- Worker ---
    "poll_interval_seconds": _fm(
        "Worker",
        "Poll Interval (seconds)",
        "number",
        restart="component",
        help="Seconds between inbox polls (0 = disabled)",
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
