"""API-key validation and rate limiting for MCP tools."""

from __future__ import annotations

import time

import structlog
from mcp.server.fastmcp import Context

from app.config import settings

log = structlog.get_logger(__name__)


class RateLimiter:
    """Simple sliding-window rate limiter (per-hour)."""

    def __init__(self, max_per_hour: int) -> None:
        self.max_per_hour = max_per_hour
        self._timestamps: dict[str, list[float]] = {}

    def check(self, key: str) -> None:
        """Raise ValueError if the rate limit for *key* is exceeded."""
        if self.max_per_hour <= 0:
            return  # unlimited

        now = time.monotonic()
        window = 3600.0  # 1 hour
        stamps = self._timestamps.setdefault(key, [])

        # Prune old entries
        stamps[:] = [t for t in stamps if now - t < window]

        if len(stamps) >= self.max_per_hour:
            log.warning("rate limit exceeded", key=key, limit=self.max_per_hour)
            raise ValueError(
                f"Rate limit exceeded: max {self.max_per_hour} calls per hour for '{key}'. "
                f"Try again later."
            )
        stamps.append(now)


def check_api_key(ctx: Context) -> None:
    """Validate the MCP API key if one is configured.

    When ``MCP_API_KEY`` is set, every tool call must include a matching
    ``x-api-key`` header (for HTTP transports) **or** a matching
    ``_api_key`` parameter.  For stdio transport with no key configured
    this is a no-op.
    """
    expected = settings.mcp_api_key
    if not expected:
        return  # no auth configured

    # Try to extract key from request metadata
    meta = getattr(ctx.request_context, "meta", None)
    provided = None
    if meta and hasattr(meta, "headers"):
        headers = meta.headers or {}
        provided = headers.get("x-api-key")

    if provided != expected:
        log.warning("api key mismatch")
        raise ValueError("Invalid or missing API key.")
