"""Pydantic models for internal data structures and LLM I/O."""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field


# =============================================================================
# Paperless API DTOs (subset - only fields we actually use)
# =============================================================================
class PaperlessDocument(BaseModel):
    id: int
    title: str
    content: str = ""
    created: datetime | None = None
    created_date: str | None = None
    modified: datetime | None = None
    added: datetime | None = None
    correspondent: int | None = None
    document_type: int | None = None
    storage_path: int | None = None
    tags: list[int] = Field(default_factory=list)

    class Config:
        extra = "ignore"


class PaperlessEntity(BaseModel):
    """Generic for tags, correspondents, document_types, storage_paths."""

    id: int
    name: str
    slug: str | None = None
    match: str | None = None
    matching_algorithm: int | None = None

    class Config:
        extra = "ignore"


# =============================================================================
# LLM structured output
# =============================================================================
class ProposedTag(BaseModel):
    name: str
    confidence: int = 50  # 0-100


class ClassificationResult(BaseModel):
    """Strict schema returned by the LLM."""

    title: str
    date: str | None = None  # ISO date YYYY-MM-DD
    correspondent: str | None = None
    document_type: str | None = None
    storage_path: str | None = None
    tags: list[ProposedTag] = Field(default_factory=list)
    confidence: int = 50
    reasoning: str = ""


# =============================================================================
# DB-backed suggestion record
# =============================================================================
SuggestionStatus = Literal["pending", "accepted", "rejected", "committed", "error"]


class SuggestionRow(BaseModel):
    id: int
    document_id: int
    created_at: str
    status: SuggestionStatus
    confidence: int | None = None
    reasoning: str | None = None

    original_title: str | None = None
    original_date: str | None = None
    original_correspondent: int | None = None
    original_doctype: int | None = None
    original_storage_path: int | None = None
    original_tags_json: str | None = None

    proposed_title: str | None = None
    proposed_date: str | None = None
    proposed_correspondent_name: str | None = None
    proposed_correspondent_id: int | None = None
    proposed_doctype_name: str | None = None
    proposed_doctype_id: int | None = None
    proposed_storage_path_name: str | None = None
    proposed_storage_path_id: int | None = None
    proposed_tags_json: str | None = None

    raw_response: str | None = None
    context_docs_json: str | None = None


# =============================================================================
# Tag whitelist
# =============================================================================
class TagWhitelistEntry(BaseModel):
    name: str
    paperless_id: int | None = None
    approved: bool = False
    first_seen: str
    times_seen: int = 1
    notes: str | None = None


# =============================================================================
# Review form payload from the GUI
# =============================================================================
class ReviewDecision(BaseModel):
    suggestion_id: int
    title: str
    date: str | None = None
    correspondent_id: int | None = None
    doctype_id: int | None = None
    storage_path_id: int | None = None
    tag_ids: list[int] = Field(default_factory=list)
    action: Literal["accept", "reject"]
