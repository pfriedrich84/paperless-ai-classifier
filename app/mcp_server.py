"""MCP server exposing Paperless-NGX operations and AI classification tools."""

from __future__ import annotations

import logging
import sys
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import structlog
from mcp.server.fastmcp import FastMCP

from app.clients.meilisearch import MeiliClient
from app.clients.ollama import OllamaClient
from app.clients.paperless import PaperlessClient
from app.config import settings
from app.db import EMBED_DIM, init_db
from app.mcp_tools._auth import RateLimiter
from app.mcp_tools._deps import Deps


def _configure_logging() -> None:
    """Set up structlog — stderr only, so stdio transport stays clean."""
    log_level = getattr(logging, settings.log_level.upper(), logging.INFO)

    # In stdio mode, stdout is reserved for MCP JSON-RPC messages.
    log_output = sys.stderr if settings.mcp_transport == "stdio" else sys.stdout

    structlog.configure(
        processors=[
            structlog.contextvars.merge_contextvars,
            structlog.processors.add_log_level,
            structlog.processors.StackInfoRenderer(),
            structlog.dev.set_exc_info,
            structlog.processors.TimeStamper(fmt="iso"),
            structlog.processors.JSONRenderer(),
        ],
        wrapper_class=structlog.make_filtering_bound_logger(log_level),
        context_class=dict,
        logger_factory=structlog.PrintLoggerFactory(file=log_output),
        cache_logger_on_first_use=True,
    )


@asynccontextmanager
async def lifespan(server: FastMCP) -> AsyncIterator[Deps]:
    """Initialize clients and DB, yield Deps, cleanup on shutdown."""
    _configure_logging()
    log = structlog.get_logger("mcp_server")
    log.info("starting MCP server")

    init_db()

    paperless = PaperlessClient()
    ollama = OllamaClient()
    meili = MeiliClient()
    rate_limiter = RateLimiter(max_per_hour=settings.mcp_classify_rate_limit)

    if not await paperless.ping():
        log.warning("paperless not reachable at startup")
    if not await ollama.ping():
        log.warning("ollama not reachable at startup")
    if await meili.ping():
        await meili.ensure_index(EMBED_DIM)
    else:
        log.warning("meilisearch not reachable at startup")

    try:
        yield Deps(paperless=paperless, ollama=ollama, meili=meili, rate_limiter=rate_limiter)
    finally:
        await meili.aclose()
        await paperless.aclose()
        await ollama.aclose()
        log.info("MCP server shutdown complete")


mcp = FastMCP(
    name="paperless-ai-classifier",
    instructions=(
        "MCP server for Paperless-NGX document management and AI classification. "
        "Use tools to search and browse documents, list entities (tags, correspondents, "
        "document types), run AI classification on inbox documents, review suggestions, "
        "and manage tag proposals."
    ),
    lifespan=lifespan,
    host=settings.mcp_host,
    port=settings.mcp_port,
)

# ---------------------------------------------------------------------------
# Register tools — each module exposes register(mcp)
# ---------------------------------------------------------------------------
from app.mcp_tools.classify import register as register_classify  # noqa: E402
from app.mcp_tools.documents import register as register_documents  # noqa: E402
from app.mcp_tools.entities import register as register_entities  # noqa: E402
from app.mcp_tools.resources import register as register_resources  # noqa: E402
from app.mcp_tools.suggestions import register as register_suggestions  # noqa: E402
from app.mcp_tools.system import register as register_system  # noqa: E402
from app.mcp_tools.tags import register as register_tags  # noqa: E402

register_entities(mcp)
register_system(mcp)
register_resources(mcp)
register_documents(mcp)
register_classify(mcp)
register_suggestions(mcp)
register_tags(mcp)


if __name__ == "__main__":
    mcp.run(transport=settings.mcp_transport)  # type: ignore[arg-type]
