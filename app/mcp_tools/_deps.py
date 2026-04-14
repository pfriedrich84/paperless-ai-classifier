"""Typed dependency container for MCP tool context."""

from __future__ import annotations

from dataclasses import dataclass

from mcp.server.fastmcp import Context

from app.clients.meilisearch import MeiliClient
from app.clients.ollama import OllamaClient
from app.clients.paperless import PaperlessClient
from app.mcp_tools._auth import RateLimiter


@dataclass
class Deps:
    paperless: PaperlessClient
    ollama: OllamaClient
    meili: MeiliClient
    rate_limiter: RateLimiter


def get_deps(ctx: Context) -> Deps:
    """Extract Deps from the MCP lifespan context."""
    return ctx.request_context.lifespan_context
