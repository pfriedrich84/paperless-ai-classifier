"""Tests for retroactive correspondent/doctype application on approval."""

from __future__ import annotations

import sqlite3

import pytest

from app.models import PaperlessDocument
from app.pipeline.committer import retroactive_correspondent_apply, retroactive_doctype_apply


@pytest.mark.asyncio
async def test_retroactive_correspondent_case_insensitive(mock_paperless, patch_db, tmp_db):
    """Correspondent matching should be case-insensitive (like tags)."""
    conn = sqlite3.connect(str(tmp_db))
    conn.row_factory = sqlite3.Row
    conn.execute(
        """INSERT INTO suggestions
           (document_id, status, proposed_title, proposed_correspondent_name, proposed_correspondent_id)
           VALUES (42, 'committed', 'Test Doc', 'tk', NULL)"""
    )
    conn.commit()
    conn.close()

    mock_paperless.get_document.return_value = PaperlessDocument(
        id=42, title="Test", correspondent=20
    )

    patched, pending = await retroactive_correspondent_apply("TK", 50, mock_paperless)

    assert patched == 1
    assert pending == 0
    mock_paperless.patch_document.assert_called_once_with(42, {"correspondent": 50})


@pytest.mark.asyncio
async def test_retroactive_doctype_case_insensitive(mock_paperless, patch_db, tmp_db):
    """Document type matching should be case-insensitive (like tags)."""
    conn = sqlite3.connect(str(tmp_db))
    conn.row_factory = sqlite3.Row
    conn.execute(
        """INSERT INTO suggestions
           (document_id, status, proposed_title, proposed_doctype_name, proposed_doctype_id)
           VALUES (42, 'committed', 'Test Doc', 'rechnung', NULL)"""
    )
    conn.commit()
    conn.close()

    mock_paperless.get_document.return_value = PaperlessDocument(
        id=42, title="Test", document_type=10
    )

    patched, pending = await retroactive_doctype_apply("Rechnung", 77, mock_paperless)

    assert patched == 1
    assert pending == 0
    mock_paperless.patch_document.assert_called_once_with(42, {"document_type": 77})
