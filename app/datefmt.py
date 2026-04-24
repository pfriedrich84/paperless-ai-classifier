"""Date formatting helpers for GUI display."""

from __future__ import annotations

from datetime import datetime

from app.config import settings


def format_date(value: str | None, fmt: str | None = None) -> str:
    """Format ISO-like dates for GUI display.

    Supports plain ``YYYY-MM-DD`` values and falls back to the original string
    when parsing fails.
    """
    if not value:
        return "—"

    text = str(value).strip()
    if not text:
        return "—"

    display_fmt = fmt or settings.gui_date_format

    for parser in ("%Y-%m-%d", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%dT%H:%M:%S.%f"):
        try:
            return datetime.strptime(text, parser).strftime(display_fmt)
        except ValueError:
            continue

    try:
        return datetime.fromisoformat(text.replace("Z", "+00:00")).strftime(display_fmt)
    except ValueError:
        return text
