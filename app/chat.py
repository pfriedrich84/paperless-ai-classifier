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

from app.clients.ollama import OllamaClient
from app.clients.paperless import PaperlessClient
from app.config import settings
from app.models import PaperlessEntity
from app.pipeline.classifier import (
    _estimate_tokens,
    _format_context_block,
    _tokens_to_chars,
)
from app.pipeline.context_builder import SimilarDocument, find_similar_by_query_text

log = structlog.get_logger(__name__)

SESSION_TTL = 3600  # 1 hour
MAX_HISTORY = 20  # max messages per session (10 exchanges)


# ---------------------------------------------------------------------------
# Session management
# ---------------------------------------------------------------------------
@dataclass
class _EntityCache:
    """Cached Paperless entity lists — fetched once per session."""

    correspondents: list[PaperlessEntity] = field(default_factory=list)
    doctypes: list[PaperlessEntity] = field(default_factory=list)
    storage_paths: list[PaperlessEntity] = field(default_factory=list)
    tags: list[PaperlessEntity] = field(default_factory=list)
    loaded: bool = False


@dataclass
class ChatSession:
    messages: list[dict[str, str]] = field(default_factory=list)
    last_active: float = field(default_factory=time.time)
    entity_cache: _EntityCache = field(default_factory=_EntityCache)


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


def _budget_context_blocks(
    similar: list[SimilarDocument],
    system_prompt: str,
    history: list[dict[str, str]],
    question: str,
    correspondents: list[PaperlessEntity],
    doctypes: list[PaperlessEntity],
    storage_paths: list[PaperlessEntity],
    tags: list[PaperlessEntity],
) -> str:
    """Build context text for chat using dynamic token budgeting.

    Mirrors the allocation strategy from ``classifier.build_user_prompt``:
    estimate token usage for fixed parts (system prompt, history, question)
    and distribute the remaining budget across context documents.
    """
    if not similar:
        return ""

    RESPONSE_RESERVE = 512
    MIN_DOC_TOKENS = 100
    num_ctx = settings.ollama_num_ctx

    system_tokens = _estimate_tokens(system_prompt)
    history_tokens = sum(_estimate_tokens(m["content"]) for m in history)
    question_tokens = _estimate_tokens(question)
    fixed_tokens = system_tokens + history_tokens + question_tokens + 80  # overhead

    available_tokens = int((num_ctx - RESPONSE_RESERVE - fixed_tokens) * 0.85)
    if available_tokens < 200:
        available_tokens = 200

    active = list(similar)
    while active:
        per_doc = available_tokens // len(active)
        if per_doc >= MIN_DOC_TOKENS:
            break
        active.pop()  # drop least-similar (last) doc

    if not active:
        return ""

    chars_per_doc = _tokens_to_chars(available_tokens // len(active))

    blocks = []
    for sim in active:
        block = _format_context_block(
            sim.document, chars_per_doc, correspondents, doctypes, storage_paths, tags
        )
        blocks.append(block)
    return "\n".join(blocks)


async def _ensure_entity_cache(session: ChatSession, paperless: PaperlessClient) -> _EntityCache:
    """Fetch entity lists once per session, then reuse from cache."""
    cache = session.entity_cache
    if cache.loaded:
        return cache
    try:
        cache.correspondents = await paperless.list_correspondents()
        cache.doctypes = await paperless.list_document_types()
        cache.storage_paths = await paperless.list_storage_paths()
        cache.tags = await paperless.list_tags()
    except Exception as exc:
        log.warning("failed to fetch entity lists for chat", error=str(exc))
    cache.loaded = True
    return cache


# ---------------------------------------------------------------------------
# RAG pipeline
# ---------------------------------------------------------------------------
async def ask(
    question: str,
    session: ChatSession,
    paperless: PaperlessClient,
    ollama: OllamaClient,
) -> ChatResult:
    """Full RAG pipeline: embed -> vector search -> format context -> LLM -> answer.

    Appends the plain Q&A to *session.messages* (not the context-augmented
    prompt) so history stays compact.
    """
    # 1. Find similar documents via vector search
    similar = await find_similar_by_query_text(
        question, paperless, ollama, limit=settings.context_max_docs
    )

    # 2. Build context block with dynamic token budgeting
    system_prompt = load_chat_system_prompt()
    context_text = ""
    if similar:
        entities = await _ensure_entity_cache(session, paperless)
        context_text = _budget_context_blocks(
            similar,
            system_prompt,
            session.messages,
            question,
            entities.correspondents,
            entities.doctypes,
            entities.storage_paths,
            entities.tags,
        )

    # 3. Build messages list
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
        answer = "Fehler bei der Verarbeitung. Bitte später erneut versuchen."

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
