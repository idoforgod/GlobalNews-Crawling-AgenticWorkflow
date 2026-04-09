"""SQLite storage for sermon research data."""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime
from pathlib import Path

DB_PATH = Path(__file__).parent.parent / "sermons" / "sermon_research.db"


def _get_conn() -> sqlite3.Connection:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(DB_PATH))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    return conn


def init_db() -> None:
    """Initialize the database schema."""
    conn = _get_conn()
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS researches (
            id TEXT PRIMARY KEY,
            reference TEXT NOT NULL,
            analysis TEXT NOT NULL,
            outline TEXT NOT NULL,
            additional_notes TEXT DEFAULT '',
            markdown_path TEXT,
            created_at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS conversations (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            research_id TEXT NOT NULL,
            role TEXT NOT NULL,
            content TEXT NOT NULL,
            created_at TEXT NOT NULL,
            FOREIGN KEY (research_id) REFERENCES researches(id)
        );

        CREATE VIRTUAL TABLE IF NOT EXISTS researches_fts USING fts5(
            reference, analysis, outline, additional_notes,
            content='researches',
            content_rowid='rowid'
        );

        CREATE TRIGGER IF NOT EXISTS researches_ai AFTER INSERT ON researches BEGIN
            INSERT INTO researches_fts(rowid, reference, analysis, outline, additional_notes)
            VALUES (new.rowid, new.reference, new.analysis, new.outline, new.additional_notes);
        END;
    """)
    conn.close()


def save_research(
    research_id: str,
    reference: str,
    analysis: str,
    outline: str,
    markdown_path: str = "",
    additional_notes: str = "",
) -> None:
    """Save a sermon research to the database."""
    conn = _get_conn()
    conn.execute(
        """INSERT OR REPLACE INTO researches
           (id, reference, analysis, outline, additional_notes, markdown_path, created_at)
           VALUES (?, ?, ?, ?, ?, ?, ?)""",
        (research_id, reference, analysis, outline, additional_notes,
         markdown_path, datetime.now().isoformat()),
    )
    conn.commit()
    conn.close()


def save_conversation(research_id: str, role: str, content: str) -> None:
    """Save a conversation message."""
    conn = _get_conn()
    conn.execute(
        "INSERT INTO conversations (research_id, role, content, created_at) VALUES (?, ?, ?, ?)",
        (research_id, role, content, datetime.now().isoformat()),
    )
    conn.commit()
    conn.close()


def get_all_researches() -> list[dict]:
    """Get all researches ordered by date."""
    conn = _get_conn()
    rows = conn.execute(
        "SELECT id, reference, created_at FROM researches ORDER BY created_at DESC"
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def get_research(research_id: str) -> dict | None:
    """Get a specific research by ID."""
    conn = _get_conn()
    row = conn.execute(
        "SELECT * FROM researches WHERE id = ?", (research_id,)
    ).fetchone()
    conn.close()
    return dict(row) if row else None


def search_researches(query: str) -> list[dict]:
    """Full-text search across all researches."""
    conn = _get_conn()
    rows = conn.execute(
        """SELECT r.id, r.reference, r.created_at,
                  snippet(researches_fts, 1, '<mark>', '</mark>', '...', 30) as snippet
           FROM researches_fts fts
           JOIN researches r ON r.rowid = fts.rowid
           WHERE researches_fts MATCH ?
           ORDER BY rank""",
        (query,),
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def get_conversations(research_id: str) -> list[dict]:
    """Get conversation history for a research."""
    conn = _get_conn()
    rows = conn.execute(
        "SELECT role, content, created_at FROM conversations WHERE research_id = ? ORDER BY created_at",
        (research_id,),
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]
