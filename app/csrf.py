"""Simple double-submit-cookie CSRF protection for browser POST routes."""

from __future__ import annotations

import secrets

from fastapi import Request
from fastapi.responses import HTMLResponse, JSONResponse, Response
from starlette.middleware.base import BaseHTTPMiddleware

from app.request_security import is_https_request

CSRF_COOKIE_NAME = "csrf_token"
CSRF_HEADER_NAME = "X-CSRF-Token"
CSRF_FORM_FIELD = "csrf_token"
_SAFE_METHODS = {"GET", "HEAD", "OPTIONS", "TRACE"}
_EXEMPT_PATH_PREFIXES = ("/webhook", "/healthz", "/static")


class CSRFMiddleware(BaseHTTPMiddleware):
    """Protect unsafe requests with a double-submit CSRF token."""

    async def dispatch(self, request: Request, call_next):
        csrf_token = request.cookies.get(CSRF_COOKIE_NAME) or secrets.token_urlsafe(32)
        request.state.csrf_token = csrf_token

        if request.method not in _SAFE_METHODS and not request.url.path.startswith(
            _EXEMPT_PATH_PREFIXES
        ):
            submitted = request.headers.get(CSRF_HEADER_NAME)
            if not submitted:
                try:
                    form = await request.form()
                    submitted = form.get(CSRF_FORM_FIELD)
                except Exception:
                    submitted = None

            if not submitted or not secrets.compare_digest(submitted, csrf_token):
                return _csrf_rejected_response(request)

        response = await call_next(request)
        _set_csrf_cookie(request, response, csrf_token)
        return response


def _set_csrf_cookie(request: Request, response: Response, csrf_token: str) -> None:
    response.set_cookie(
        CSRF_COOKIE_NAME,
        csrf_token,
        httponly=False,
        samesite="lax",
        secure=is_https_request(request),
        path="/",
    )


def _csrf_rejected_response(request: Request) -> Response:
    if request.headers.get("HX-Request") == "true":
        return HTMLResponse(
            '<div class="text-red-600 text-sm font-medium mt-2">Invalid or missing CSRF token.</div>',
            status_code=403,
        )
    if "application/json" in request.headers.get("accept", ""):
        return JSONResponse(status_code=403, content={"detail": "Invalid or missing CSRF token"})
    return HTMLResponse("Invalid or missing CSRF token.", status_code=403)
