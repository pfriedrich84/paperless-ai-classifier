"""Persistent config: read/write {DATA_DIR}/config.env and hot-reload settings."""

from __future__ import annotations

import os
import shutil
from collections import OrderedDict
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import structlog

from app.config import FIELD_META, Settings, settings

log = structlog.get_logger(__name__)

# Fields that require a full app restart (not hot-reloadable)
_RESTART_REQUIRED_FIELDS = {
    name for name, meta in FIELD_META.items() if meta.get("restart") == "app"
}


def config_env_path() -> Path:
    """Return path to the persistent config.env inside the data directory."""
    return Path(settings.data_dir) / "config.env"


def read_env_file(path: Path) -> OrderedDict[str, str]:
    """Parse a KEY=VALUE env file.  Skips comments and blank lines."""
    result: OrderedDict[str, str] = OrderedDict()
    if not path.is_file():
        return result
    for line in path.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        if "=" not in stripped:
            continue
        key, _, value = stripped.partition("=")
        result[key.strip()] = value.strip()
    return result


def write_env_file(path: Path, values: OrderedDict[str, str]) -> None:
    """Atomic write of KEY=VALUE pairs.  Creates a .bak before overwriting."""
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.is_file():
        ts = datetime.now(tz=UTC).strftime("%Y%m%d%H%M%S")
        backup = path.with_suffix(f".bak.{ts}")
        shutil.copy2(path, backup)

    tmp = path.with_suffix(".tmp")
    lines = [f"{k}={v}\n" for k, v in values.items()]
    tmp.write_text("".join(lines), encoding="utf-8")
    os.replace(str(tmp), str(path))
    log.info("config.env written", path=str(path), keys=len(values))


def save_config(updates: dict[str, Any]) -> tuple[dict[str, Any], set[str]]:
    """Merge *updates* into config.env, update the in-memory singleton.

    Returns ``(changed, restart_required)`` where *changed* maps field names
    to their new values and *restart_required* lists any fields that need an
    app restart to take effect.
    """
    path = config_env_path()
    existing = read_env_file(path)

    changed: dict[str, Any] = {}
    restart_required: set[str] = set()

    for key, new_value in updates.items():
        if key not in Settings.model_fields:
            continue
        env_key = key.upper()
        str_val = str(new_value)
        old_val = existing.get(env_key)
        current_val = getattr(settings, key)

        # Normalise booleans for comparison
        if isinstance(current_val, bool):
            new_bool = str_val.lower() in ("true", "1", "yes")
            if new_bool == current_val and old_val is not None:
                continue
            str_val = str(new_bool).lower()
            new_value = new_bool
        elif isinstance(current_val, int):
            try:
                new_int = int(str_val)
            except ValueError:
                continue
            if new_int == current_val and old_val is not None:
                continue
            str_val = str(new_int)
            new_value = new_int
        elif isinstance(current_val, float):
            try:
                new_float = float(str_val)
            except ValueError:
                continue
            if new_float == current_val and old_val is not None:
                continue
            str_val = str(new_float)
            new_value = new_float
        else:
            if str_val == str(current_val) and old_val is not None:
                continue

        existing[env_key] = str_val
        changed[key] = new_value

        # Update in-memory singleton
        object.__setattr__(settings, key, new_value)

        if key in _RESTART_REQUIRED_FIELDS:
            restart_required.add(key)

    if changed:
        write_env_file(path, existing)
        log.info("config saved", changed=list(changed.keys()))

    return changed, restart_required


async def apply_runtime_changes(app: Any, changed: dict[str, Any]) -> list[str]:
    """Recreate clients / reschedule jobs for changed fields.

    Returns a list of human-readable actions taken.
    """
    actions: list[str] = []
    changed_keys = set(changed.keys())

    # --- Paperless client ---
    paperless_fields = {"paperless_url", "paperless_token", "paperless_inbox_tag_id"}
    if changed_keys & paperless_fields:
        from app.clients.paperless import PaperlessClient

        old = getattr(app.state, "paperless", None)
        if old:
            await old.aclose()
        app.state.paperless = PaperlessClient()
        actions.append("Paperless client recreated")

    # --- Ollama client ---
    ollama_fields = {
        "ollama_url",
        "ollama_model",
        "ollama_embed_model",
        "ollama_embed_dim",
        "ollama_ocr_model",
        "ocr_vision_model",
        "ollama_timeout_seconds",
        "ollama_num_ctx",
        "ollama_embed_num_ctx",
        "ollama_ocr_num_ctx",
        "ollama_model_swap_delay",
    }
    if changed_keys & ollama_fields:
        from app.clients.ollama import OllamaClient

        old = getattr(app.state, "ollama", None)
        if old:
            await old.aclose()
        app.state.ollama = OllamaClient()
        actions.append("Ollama client recreated")

    # Keep worker module refs in sync after client recreation.
    if (changed_keys & paperless_fields) or (changed_keys & ollama_fields):
        from app.worker import set_clients

        set_clients(getattr(app.state, "paperless", None), getattr(app.state, "ollama", None))
        actions.append("Worker clients updated")

    # --- Telegram client ---
    telegram_fields = {"enable_telegram", "telegram_bot_token", "telegram_chat_id"}
    if changed_keys & telegram_fields:
        from app.clients.telegram import TelegramClient
        from app.telegram_handler import start_telegram, stop_telegram

        stop_telegram()
        old = getattr(app.state, "telegram", None)
        if old:
            await old.aclose()
        new_tg = TelegramClient()
        app.state.telegram = new_tg
        paperless = getattr(app.state, "paperless", None)
        ollama = getattr(app.state, "ollama", None)
        if paperless:
            start_telegram(new_tg, paperless, ollama)
        actions.append("Telegram client recreated")

    # --- Scheduler ---
    if "poll_interval_seconds" in changed_keys:
        scheduler = getattr(app.state, "scheduler", None)
        if settings.poll_interval_seconds <= 0:
            if scheduler:
                scheduler.pause_job("poll_inbox")
                actions.append("Automatic polling disabled")
        elif scheduler:
            scheduler.reschedule_job(
                "poll_inbox",
                trigger="interval",
                seconds=settings.poll_interval_seconds,
            )
            scheduler.resume_job("poll_inbox")
            actions.append(f"Poll interval changed to {settings.poll_interval_seconds}s")

    return actions
