"""Helpers for safely rendering small HTML fragments and user-visible errors."""

from __future__ import annotations

from html import escape
from urllib.parse import quote


def escape_html(value: object) -> str:
    """Escape a value for safe insertion into inline HTML fragments."""
    return escape(str(value), quote=True)


def encode_path_segment(value: object) -> str:
    """Encode a value for use in URLs / DOM ids derived from path segments."""
    return quote(str(value), safe="")
