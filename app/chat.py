"""Shared RAG chat core — session management and ask() pipeline.

Used by both the web route (``app.routes.chat``) and the Telegram handler
(``app.telegram_handler``) so the RAG logic lives in one place.
"""

from __future__ import annotations

import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path

import structlog

from app.clients.meilisearch import MeiliClient
from app.clients.ollama import OllamaClient
from app.clients.paperless import PaperlessClient
from app.config import settings
from app.pipeline.classifier import _format_context_block
from app.pipeline.context_builder import find_similar_by_query_text

log = structlog.get_logger(__name__)

SESSION_TTL = 3600  # 1 hour
MAX_HISTORY = 20  # max messages per session (10 exchanges)


# ---------------------------------------------------------------------------
# Session management
# ---------------------------------------------------------------------------
@dataclass
class ChatSession:
    messages: list[dict[str, str]] = field(default_factory=list)
    last_active: float = field(default_factory=time.time)


@dataclass
class ChatResult:
    answer: str
    sources: list[dict] = field(default_factory=list)  # [{id, title, distance}]


_sessions: dict[str, ChatSession] = {}


def _expire_sessions() -> None:
    """Remove sessions older than TTL."""
    now = time.time()
    expired = [sid for sid, s in _sessions.items() if now - s.last_active > SESSION_TTL]
    for sid in expired:
        del _sessions[sid]


def get_or_create_session(session_id: str | None) -> tuple[str, ChatSession]:
    """Return (session_id, session).  Creates a new session if missing/expired."""
    _expire_sessions()
    if session_id and session_id in _sessions:
        session = _sessions[session_id]
        session.last_active = time.time()
        return session_id, session
    new_id = uuid.uuid4().hex[:16]
    session = ChatSession()
    _sessions[new_id] = session
    return new_id, session


# ---------------------------------------------------------------------------
# Prompt helpers
# ---------------------------------------------------------------------------
def load_chat_system_prompt() -> str:
    """Load chat system prompt — user override in /data takes precedence."""
    override = Path(settings.data_dir) / "chat_system.txt"
    if override.is_file():
        return override.read_text(encoding="utf-8")
    return (settings.prompts_dir / "chat_system.txt").read_text(encoding="utf-8")


def _build_chat_user_message(question: str, context: str) -> str:
    """Combine user question with document context."""
    if context:
        return f"# Relevante Dokumente\n\n{context}\n\n# Frage des Benutzers\n\n{question}"
    return question


# ---------------------------------------------------------------------------
# RAG pipeline
# ---------------------------------------------------------------------------
async def ask(
    question: str,
    session: ChatSession,
    paperless: PaperlessClient,
    ollama: OllamaClient,
    meili: MeiliClient,
) -> ChatResult:
    """Full RAG pipeline: embed -> hybrid search -> format context -> LLM -> answer.

    Appends the plain Q&A to *session.messages* (not the context-augmented
    prompt) so history stays compact.
    """
    # 1. Find similar documents via hybrid search (BM25 + vector)
    similar = await find_similar_by_query_text(
        question, paperless, ollama, meili, limit=settings.context_max_docs
    )

    # 2. Build context block
    context_text = ""
    if similar:
        correspondents = await paperless.list_correspondents()
        doctypes = await paperless.list_document_types()
        storage_paths = await paperless.list_storage_paths()
        tags_list = await paperless.list_tags()

        doc_blocks = []
        for sim in similar:
            block = _format_context_block(
                sim.document, 2000, correspondents, doctypes, storage_paths, tags_list
            )
            doc_blocks.append(block)
        context_text = "\n".join(doc_blocks)

    # 3. Build messages list
    system_prompt = load_chat_system_prompt()
    user_content = _build_chat_user_message(question, context_text)

    messages: list[dict[str, str]] = [{"role": "system", "content": system_prompt}]
    for msg in session.messages:
        messages.append({"role": msg["role"], "content": msg["content"]})
    messages.append({"role": "user", "content": user_content})

    # 4. Call Ollama
    try:
        answer = await ollama.chat(messages)
    except Exception as exc:
        log.error("chat LLM call failed", error=str(exc))
        answer = f"Fehler bei der Verarbeitung: {exc}"

    # 5. Update session history (plain Q&A only)
    session.messages.append({"role": "user", "content": question})
    session.messages.append({"role": "assistant", "content": answer})
    if len(session.messages) > MAX_HISTORY:
        session.messages = session.messages[-MAX_HISTORY:]

    # 6. Build sources list
    sources = [
        {"id": s.document.id, "title": s.document.title, "distance": round(s.distance, 3)}
        for s in similar
    ]

    return ChatResult(answer=answer, sources=sources)
