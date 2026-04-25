"""Pydantic models for internal data structures and LLM I/O."""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator


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

    model_config = ConfigDict(extra="ignore")


class PaperlessEntity(BaseModel):
    """Generic for tags, correspondents, document_types, storage_paths."""

    id: int
    name: str
    slug: str | None = None
    match: str | None = None
    matching_algorithm: int | None = None

    model_config = ConfigDict(extra="ignore")


# =============================================================================
# LLM structured output
# =============================================================================
class ProposedTag(BaseModel):
    name: str
    confidence: int = 50  # 0-100

    @field_validator("confidence", mode="before")
    @classmethod
    def _coerce_confidence(cls, value: object) -> int:
        # Accept common LLM outputs like 0.9 (probability) and map to 0-100.
        if isinstance(value, str):
            try:
                value = float(value.strip())
            except ValueError:
                return 50
        if isinstance(value, int):
            return max(0, min(100, value))
        if isinstance(value, float):
            if 0.0 <= value <= 1.0:
                return max(0, min(100, round(value * 100)))
            return max(0, min(100, round(value)))
        return 50


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

    @field_validator("tags", mode="before")
    @classmethod
    def _coerce_tags(cls, value: Any) -> Any:
        """Accept common loose tag outputs from LLMs.

        Normal form is a list of objects ``[{"name": str, "confidence": int}]``.
        We also tolerate:
        - ``["tag-a", "tag-b"]``
        - mixed lists of strings + objects
        - single string ``"tag-a"``
        - object with ``tag`` key instead of ``name``
        """
        if value is None:
            return []

        if isinstance(value, str):
            name = value.strip()
            return [{"name": name}] if name else []

        if not isinstance(value, list):
            return value

        normalized: list[Any] = []
        for item in value:
            if isinstance(item, str):
                name = item.strip()
                if name:
                    normalized.append({"name": name})
                continue

            if isinstance(item, dict) and "name" not in item and "tag" in item:
                item = {**item, "name": item.get("tag")}

            normalized.append(item)

        return normalized

    @field_validator("confidence", mode="before")
    @classmethod
    def _coerce_confidence(cls, value: object) -> int:
        # Accept common LLM outputs like 0.9 (probability) and map to 0-100.
        if isinstance(value, str):
            try:
                value = float(value.strip())
            except ValueError:
                return 50
        if isinstance(value, int):
            return max(0, min(100, value))
        if isinstance(value, float):
            if 0.0 <= value <= 1.0:
                return max(0, min(100, round(value * 100)))
            return max(0, min(100, round(value)))
        return 50


JudgeVerdictType = Literal["agree", "corrected", "skipped", "error"]


class JudgeVerdict(BaseModel):
    """Outcome of an LLM-as-judge verification pass over a ClassificationResult."""

    verdict: JudgeVerdictType
    reasoning: str = ""
    # Populated only when verdict == "corrected"
    corrected: ClassificationResult | None = None


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

    # LLM-as-judge verification
    judge_verdict: str | None = None  # 'agree' | 'corrected' | 'skipped' | 'error'
    judge_reasoning: str | None = None
    original_proposed_json: str | None = None  # snapshot of first-pass when corrected

    @property
    def effective_date(self) -> str | None:
        return self.proposed_date if self.proposed_date is not None else self.original_date

    @property
    def effective_correspondent_id(self) -> int | None:
        return (
            self.proposed_correspondent_id
            if self.proposed_correspondent_id is not None
            else self.original_correspondent
        )

    @property
    def effective_doctype_id(self) -> int | None:
        return (
            self.proposed_doctype_id
            if self.proposed_doctype_id is not None
            else self.original_doctype
        )

    @property
    def effective_storage_path_id(self) -> int | None:
        return (
            self.proposed_storage_path_id
            if self.proposed_storage_path_id is not None
            else self.original_storage_path
        )


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


class TagBlacklistEntry(BaseModel):
    name: str
    rejected_at: str
    times_seen: int = 1
    notes: str | None = None


# =============================================================================
# Correspondent whitelist
# =============================================================================
class CorrespondentWhitelistEntry(BaseModel):
    name: str
    paperless_id: int | None = None
    approved: bool = False
    first_seen: str
    times_seen: int = 1
    notes: str | None = None


class CorrespondentBlacklistEntry(BaseModel):
    name: str
    rejected_at: str
    times_seen: int = 1
    notes: str | None = None


# =============================================================================
# Document type whitelist
# =============================================================================
class DoctypeWhitelistEntry(BaseModel):
    name: str
    paperless_id: int | None = None
    approved: bool = False
    first_seen: str
    times_seen: int = 1
    notes: str | None = None


class DoctypeBlacklistEntry(BaseModel):
    name: str
    rejected_at: str
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
