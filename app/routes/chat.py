"""RAG chat — ask questions about your documents."""

from __future__ import annotations

import structlog
from fastapi import APIRouter, Cookie, Form, Request
from fastapi.responses import HTMLResponse

from app.chat import ask, get_or_create_session

log = structlog.get_logger(__name__)

router = APIRouter(prefix="/chat")


@router.get("")
async def chat_page(request: Request, chat_session: str | None = Cookie(default=None)):
    """Render the chat page with conversation history."""
    session_id, session = get_or_create_session(chat_session)
    response = request.app.state.templates.TemplateResponse(
        request,
        "chat.html",
        {"messages": session.messages},
    )
    response.set_cookie("chat_session", session_id, httponly=True, samesite="lax")
    return response


@router.post("/send")
async def chat_send(
    request: Request,
    question: str = Form(...),
    chat_session: str | None = Cookie(default=None),
):
    """Process a chat question via the RAG pipeline."""
    session_id, session = get_or_create_session(chat_session)
    paperless = request.app.state.paperless
    ollama = request.app.state.ollama
    meili = request.app.state.meili

    result = await ask(question, session, paperless, ollama, meili)

    tmpl = request.app.state.templates.get_template("partials/chat_messages.html")
    html = tmpl.render(messages=session.messages, sources=result.sources)
    response = HTMLResponse(html)
    response.set_cookie("chat_session", session_id, httponly=True, samesite="lax")
    return response


@router.post("/clear")
async def chat_clear(
    request: Request,
    chat_session: str | None = Cookie(default=None),
):
    """Clear conversation history."""
    if chat_session:
        _, session = get_or_create_session(chat_session)
        session.messages.clear()
    return HTMLResponse('<div id="chat-messages" class="p-4 space-y-4"></div>')
