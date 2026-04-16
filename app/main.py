"""FastAPI application entry point with lifespan, routing, and auth."""

from __future__ import annotations

import logging
import secrets
import time
import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

import structlog
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from fastapi.security import HTTPBasic
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.middleware.base import BaseHTTPMiddleware

from app.clients.ollama import OllamaClient
from app.clients.paperless import PaperlessClient
from app.clients.telegram import TelegramClient
from app.config import needs_setup, settings
from app.db import init_db
from app.telegram_handler import start_telegram, stop_telegram
from app.worker import start_scheduler, stop_scheduler

log = structlog.get_logger(__name__)

# ---------------------------------------------------------------------------
# Directories
# ---------------------------------------------------------------------------
_BASE_DIR = Path(__file__).parent
_TEMPLATES_DIR = _BASE_DIR / "templates"
_STATIC_DIR = _BASE_DIR / "static"

templates = Jinja2Templates(directory=str(_TEMPLATES_DIR))

# ---------------------------------------------------------------------------
# Logging setup
# ---------------------------------------------------------------------------


def _configure_logging() -> None:
    """Set up structlog with appropriate renderer and route third-party loggers."""
    log_level = getattr(logging, settings.log_level.upper(), logging.INFO)

    structlog.configure(
        processors=[
            structlog.contextvars.merge_contextvars,
            structlog.processors.add_log_level,
            structlog.processors.StackInfoRenderer(),
            structlog.dev.set_exc_info,
            structlog.processors.TimeStamper(fmt="iso"),
            (
                structlog.dev.ConsoleRenderer()
                if settings.log_level.upper() == "DEBUG"
                else structlog.processors.JSONRenderer()
            ),
        ],
        wrapper_class=structlog.make_filtering_bound_logger(log_level),
        context_class=dict,
        logger_factory=structlog.PrintLoggerFactory(),
        cache_logger_on_first_use=True,
    )

    # Route third-party loggers through structlog at appropriate levels
    for name in ("uvicorn", "uvicorn.access", "uvicorn.error", "httpx", "apscheduler"):
        stdlib_logger = logging.getLogger(name)
        stdlib_logger.handlers.clear()
        stdlib_logger.propagate = False
        handler = logging.StreamHandler()
        handler.setFormatter(
            structlog.stdlib.ProcessorFormatter(
                processor=structlog.dev.ConsoleRenderer()
                if settings.log_level.upper() == "DEBUG"
                else structlog.processors.JSONRenderer(),
            )
        )
        stdlib_logger.addHandler(handler)
        stdlib_logger.setLevel(log_level)


# ---------------------------------------------------------------------------
# Middleware: Request logging + request ID
# ---------------------------------------------------------------------------
class RequestLoggingMiddleware(BaseHTTPMiddleware):
    """Log every HTTP request with method, path, status, and duration."""

    async def dispatch(self, request: Request, call_next):
        request_id = str(uuid.uuid4())[:8]
        structlog.contextvars.clear_contextvars()
        structlog.contextvars.bind_contextvars(request_id=request_id)

        start = time.monotonic()
        response = await call_next(request)
        duration_ms = round((time.monotonic() - start) * 1000, 1)

        # Skip noisy healthcheck and static logs
        path = request.url.path
        if path not in ("/healthz",) and not path.startswith("/static"):
            log.info(
                "request",
                method=request.method,
                path=path,
                status=response.status_code,
                duration_ms=duration_ms,
            )

        response.headers["X-Request-ID"] = request_id
        return response


# ---------------------------------------------------------------------------
# Middleware: Security headers
# ---------------------------------------------------------------------------
class SecurityHeadersMiddleware(BaseHTTPMiddleware):
    """Add basic security headers to every response."""

    async def dispatch(self, request: Request, call_next):
        response = await call_next(request)
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["Referrer-Policy"] = "strict-origin-when-cross-origin"
        return response


# ---------------------------------------------------------------------------
# Middleware: Setup redirect (first-run wizard)
# ---------------------------------------------------------------------------
class SetupRedirectMiddleware(BaseHTTPMiddleware):
    """Redirect all non-setup traffic to /setup when essential config is missing."""

    async def dispatch(self, request: Request, call_next):
        path = request.url.path
        if needs_setup() and not (
            path.startswith("/setup") or path.startswith("/static") or path in ("/healthz",)
        ):
            from starlette.responses import RedirectResponse

            return RedirectResponse(url="/setup", status_code=302)
        return await call_next(request)


# ---------------------------------------------------------------------------
# Optional Basic Auth
# ---------------------------------------------------------------------------
security = HTTPBasic(auto_error=False)


class BasicAuthMiddleware(BaseHTTPMiddleware):
    """Simple HTTP Basic Auth protecting all routes except /healthz and /static."""

    async def dispatch(self, request: Request, call_next):
        path = request.url.path
        if path in ("/healthz",) or path.startswith("/static"):
            return await call_next(request)

        # Webhook has its own auth via WEBHOOK_SECRET
        if path.startswith("/webhook"):
            return await call_next(request)

        # Setup wizard is accessible without auth (guarded by its own flow)
        if path.startswith("/setup"):
            return await call_next(request)

        auth = request.headers.get("Authorization")
        if auth and auth.startswith("Basic "):
            import base64

            try:
                decoded = base64.b64decode(auth[6:]).decode("utf-8")
                username, password = decoded.split(":", 1)
                correct_user = secrets.compare_digest(username, settings.gui_username)
                correct_pass = secrets.compare_digest(password, settings.gui_password)
                if correct_user and correct_pass:
                    return await call_next(request)
            except Exception as exc:
                log.warning("basic auth decode error", error=str(exc), path=path)

        return JSONResponse(
            status_code=401,
            content={"detail": "Unauthorized"},
            headers={"WWW-Authenticate": 'Basic realm="archibot"'},
        )


# ---------------------------------------------------------------------------
# Lifespan
# ---------------------------------------------------------------------------
@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    _configure_logging()
    log.info("starting archibot")

    init_db()
    app.state.templates = templates

    if needs_setup():
        log.info("essential config missing — entering setup mode")
        app.state.paperless = None
        app.state.ollama = None
        app.state.telegram = None
        yield
        log.info("shutdown complete (setup mode)")
        return

    paperless = PaperlessClient()
    ollama = OllamaClient()
    telegram = TelegramClient()
    app.state.paperless = paperless
    app.state.ollama = ollama
    app.state.telegram = telegram

    # Healthchecks — warning only, don't fail startup
    if not await paperless.ping():
        log.warning("paperless not reachable at startup")
    if not await ollama.ping():
        log.warning("ollama not reachable at startup")

    start_scheduler(app)
    start_telegram(telegram, paperless, ollama)
    yield
    stop_telegram()
    stop_scheduler(app)
    await telegram.aclose()
    await paperless.aclose()
    await ollama.aclose()
    log.info("shutdown complete")


# ---------------------------------------------------------------------------
# App
# ---------------------------------------------------------------------------
app = FastAPI(
    title="ArchiBot",
    version="0.1.0",
    lifespan=lifespan,
)

# Middleware (order matters: outermost first)
app.add_middleware(SecurityHeadersMiddleware)
app.add_middleware(RequestLoggingMiddleware)
app.add_middleware(SetupRedirectMiddleware)
if settings.gui_username and settings.gui_password:
    app.add_middleware(BasicAuthMiddleware)

# Static files
app.mount("/static", StaticFiles(directory=str(_STATIC_DIR)), name="static")


# ---------------------------------------------------------------------------
# Health endpoint
# ---------------------------------------------------------------------------
@app.get("/healthz")
async def healthz():
    return {"status": "ok"}


# ---------------------------------------------------------------------------
# Routes (imported after app creation to avoid circular imports)
# ---------------------------------------------------------------------------
from app.routes import (  # noqa: E402
    chat,
    embeddings,
    errors,
    inbox,
    index,
    ocr,
    review,
    stats,
    tags,
    webhook,
)
from app.routes import settings as settings_routes  # noqa: E402
from app.routes import setup as setup_routes  # noqa: E402

app.include_router(setup_routes.router)
app.include_router(index.router)
app.include_router(chat.router)
app.include_router(inbox.router)
app.include_router(review.router)
app.include_router(tags.router)
app.include_router(ocr.router)
app.include_router(errors.router)
app.include_router(embeddings.router)
app.include_router(stats.router)
app.include_router(settings_routes.router)
app.include_router(webhook.router)
