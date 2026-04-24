"""Request security helpers for CSRF and cookie hardening."""

from __future__ import annotations

from fastapi import Request


def is_https_request(request: Request) -> bool:
    """Best-effort HTTPS detection including common reverse-proxy headers."""
    forwarded_proto = request.headers.get("x-forwarded-proto", "").split(",", 1)[0].strip()
    if forwarded_proto:
        return forwarded_proto.lower() == "https"

    forwarded = request.headers.get("forwarded", "")
    if forwarded:
        for part in forwarded.split(";"):
            key, _, value = part.partition("=")
            if key.strip().lower() == "proto":
                return value.strip().lower() == "https"

    return request.url.scheme.lower() == "https"
