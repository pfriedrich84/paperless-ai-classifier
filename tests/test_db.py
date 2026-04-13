"""Tests for database schema and operations."""

import sqlite3

import pytest


class TestSchema:
    def test_all_tables_exist(self, db_conn: sqlite3.Connection):
        """Verify all expected tables are created."""
        tables = {
            row[0]
            for row in db_conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()
        }
        expected = {
            "processed_documents",
            "suggestions",
            "tag_whitelist",
            "tag_blacklist",
            "errors",
            "doc_embedding_meta",
            "audit_log",
        }
        assert expected.issubset(tables)

    def test_suggestions_insert(self, db_conn: sqlite3.Connection):
        """Verify we can insert a suggestion and read it back."""
        db_conn.execute(
            """
            INSERT INTO suggestions (document_id, status, confidence, proposed_title)
            VALUES (1, 'pending', 85, 'Test Title')
            """
        )
        row = db_conn.execute("SELECT * FROM suggestions WHERE id = 1").fetchone()
        assert row is not None
        assert row["document_id"] == 1
        assert row["status"] == "pending"
        assert row["confidence"] == 85
        assert row["proposed_title"] == "Test Title"
        assert row["created_at"] is not None  # auto-set by default

    def test_processed_documents_insert(self, db_conn: sqlite3.Connection):
        """Verify processed_documents can be written and updated."""
        db_conn.execute(
            """
            INSERT INTO processed_documents (document_id, last_updated_at, last_processed, status)
            VALUES (42, '2024-03-15T10:00:00', '2024-03-15T10:05:00', 'pending')
            """
        )
        db_conn.execute(
            "UPDATE processed_documents SET status = 'committed' WHERE document_id = 42"
        )
        row = db_conn.execute("SELECT * FROM processed_documents WHERE document_id = 42").fetchone()
        assert row["status"] == "committed"

    def test_tag_whitelist_upsert(self, db_conn: sqlite3.Connection):
        """Verify tag whitelist insert and counter update."""
        db_conn.execute("INSERT INTO tag_whitelist (name) VALUES ('NewTag')")
        row = db_conn.execute("SELECT * FROM tag_whitelist WHERE name = 'NewTag'").fetchone()
        assert row["times_seen"] == 1
        assert row["approved"] == 0

        db_conn.execute(
            "UPDATE tag_whitelist SET times_seen = times_seen + 1 WHERE name = 'NewTag'"
        )
        row = db_conn.execute("SELECT * FROM tag_whitelist WHERE name = 'NewTag'").fetchone()
        assert row["times_seen"] == 2

    def test_errors_insert(self, db_conn: sqlite3.Connection):
        """Verify error records can be inserted."""
        db_conn.execute(
            """
            INSERT INTO errors (stage, document_id, message)
            VALUES ('classify', 42, 'Connection timeout')
            """
        )
        row = db_conn.execute("SELECT * FROM errors WHERE id = 1").fetchone()
        assert row["stage"] == "classify"
        assert row["document_id"] == 42
        assert row["occurred_at"] is not None

    def test_audit_log_insert(self, db_conn: sqlite3.Connection):
        """Verify audit log entries."""
        db_conn.execute(
            """
            INSERT INTO audit_log (action, document_id, actor, details)
            VALUES ('commit', 42, 'user', '{"title": "Test"}')
            """
        )
        row = db_conn.execute("SELECT * FROM audit_log WHERE id = 1").fetchone()
        assert row["action"] == "commit"
        assert row["actor"] == "user"

    def test_tag_blacklist_insert(self, db_conn: sqlite3.Connection):
        """Verify tag blacklist insert and rejected_at auto-set."""
        db_conn.execute("INSERT INTO tag_blacklist (name, times_seen) VALUES ('BadTag', 3)")
        row = db_conn.execute("SELECT * FROM tag_blacklist WHERE name = 'BadTag'").fetchone()
        assert row["times_seen"] == 3
        assert row["rejected_at"] is not None

    def test_tag_blacklist_primary_key(self, db_conn: sqlite3.Connection):
        """name is the primary key — duplicates should conflict."""
        db_conn.execute("INSERT INTO tag_blacklist (name) VALUES ('DupTag')")
        with pytest.raises(sqlite3.IntegrityError):
            db_conn.execute("INSERT INTO tag_blacklist (name) VALUES ('DupTag')")

    def test_processed_documents_primary_key(self, db_conn: sqlite3.Connection):
        """document_id is the primary key — duplicates should conflict."""
        db_conn.execute(
            """
            INSERT INTO processed_documents (document_id, last_updated_at, last_processed, status)
            VALUES (1, '2024-01-01', '2024-01-01', 'pending')
            """
        )
        with pytest.raises(sqlite3.IntegrityError):
            db_conn.execute(
                """
                INSERT INTO processed_documents (document_id, last_updated_at, last_processed, status)
                VALUES (1, '2024-01-02', '2024-01-02', 'pending')
                """
            )
