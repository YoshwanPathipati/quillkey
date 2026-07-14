"""SQLite storage for sessions, suggestions, and writing tips."""

import sqlite3
import threading
import uuid
from datetime import datetime, timedelta
from pathlib import Path

DB_PATH = Path(__file__).parent / "grammarly.db"

_lock = threading.Lock()
_conn: sqlite3.Connection | None = None

SCHEMA = """
CREATE TABLE IF NOT EXISTS sessions (
  id INTEGER PRIMARY KEY,
  date TEXT,
  word_count INTEGER DEFAULT 0,
  error_count INTEGER DEFAULT 0,
  accepted_count INTEGER DEFAULT 0,
  mode TEXT,
  domain TEXT
);

CREATE TABLE IF NOT EXISTS suggestions (
  id TEXT PRIMARY KEY,
  session_id INTEGER,
  error_type TEXT,
  original TEXT,
  suggestion TEXT,
  accepted INTEGER,
  timestamp TEXT
);

CREATE TABLE IF NOT EXISTS writing_tips (
  id INTEGER PRIMARY KEY,
  tip TEXT,
  category TEXT,
  shown_date TEXT
);
"""


def _get_conn() -> sqlite3.Connection:
    global _conn
    if _conn is None:
        _conn = sqlite3.connect(DB_PATH, check_same_thread=False)
        _conn.row_factory = sqlite3.Row
        _conn.executescript(SCHEMA)
        _conn.commit()
    return _conn


def init(tips: list[tuple[str, str]]) -> None:
    """Create tables and seed the tips list if empty."""
    with _lock:
        conn = _get_conn()
        count = conn.execute("SELECT COUNT(*) FROM writing_tips").fetchone()[0]
        if count == 0:
            conn.executemany(
                "INSERT INTO writing_tips (tip, category) VALUES (?, ?)", tips
            )
            conn.commit()


def create_session(mode: str, domain: str = "") -> int:
    with _lock:
        conn = _get_conn()
        cur = conn.execute(
            "INSERT INTO sessions (date, mode, domain) VALUES (?, ?, ?)",
            (datetime.now().isoformat(), mode, domain),
        )
        conn.commit()
        return cur.lastrowid


def update_session(session_id: int, word_count: int) -> None:
    with _lock:
        conn = _get_conn()
        conn.execute(
            "UPDATE sessions SET word_count = ? WHERE id = ?",
            (word_count, session_id),
        )
        conn.commit()


def log_suggestions(session_id: int, suggestions: list[dict]) -> None:
    """Store suggestions as pending (accepted = NULL); assigns each an id."""
    with _lock:
        conn = _get_conn()
        now = datetime.now().isoformat()
        for s in suggestions:
            s["id"] = s.get("id") or uuid.uuid4().hex[:12]
            conn.execute(
                "INSERT OR IGNORE INTO suggestions "
                "(id, session_id, error_type, original, suggestion, accepted, timestamp) "
                "VALUES (?, ?, ?, ?, ?, NULL, ?)",
                (
                    s["id"],
                    session_id,
                    s.get("style_type") or s["error_type"],
                    s["original"],
                    s["suggestion"],
                    now,
                ),
            )
        conn.execute(
            "UPDATE sessions SET error_count = "
            "(SELECT COUNT(*) FROM suggestions WHERE session_id = ?) WHERE id = ?",
            (session_id, session_id),
        )
        conn.commit()


def log_acceptance(suggestion_id: str, accepted: bool) -> bool:
    """Mark a suggestion accepted/ignored. Returns False if id unknown."""
    with _lock:
        conn = _get_conn()
        cur = conn.execute(
            "UPDATE suggestions SET accepted = ? WHERE id = ?",
            (1 if accepted else 0, suggestion_id),
        )
        if cur.rowcount and accepted:
            conn.execute(
                "UPDATE sessions SET accepted_count = accepted_count + 1 "
                "WHERE id = (SELECT session_id FROM suggestions WHERE id = ?)",
                (suggestion_id,),
            )
        conn.commit()
        return cur.rowcount > 0


def get_stats() -> dict:
    """Aggregate stats: errors by type, acceptance rate, writing streak."""
    with _lock:
        conn = _get_conn()
        by_type = {
            row["error_type"]: row["n"]
            for row in conn.execute(
                "SELECT error_type, COUNT(*) AS n FROM suggestions "
                "GROUP BY error_type ORDER BY n DESC"
            )
        }
        decided = conn.execute(
            "SELECT COUNT(*) FROM suggestions WHERE accepted IS NOT NULL"
        ).fetchone()[0]
        accepted = conn.execute(
            "SELECT COUNT(*) FROM suggestions WHERE accepted = 1"
        ).fetchone()[0]
        days = [
            row[0][:10]
            for row in conn.execute(
                "SELECT DISTINCT substr(date, 1, 10) FROM sessions "
                "ORDER BY 1 DESC LIMIT 60"
            )
        ]

    # Streak: consecutive days (ending today or yesterday) with a session.
    streak = 0
    cursor = datetime.now().date()
    day_set = set(days)
    if cursor.isoformat() not in day_set:
        cursor -= timedelta(days=1)
    while cursor.isoformat() in day_set:
        streak += 1
        cursor -= timedelta(days=1)

    # Improvement score: grows with total accepted fixes, weighted by rate.
    rate = (accepted / decided) if decided else 0.0
    improvement_score = round(min(100, accepted * 0.5 + rate * 50), 1)

    return {
        "errors_by_type": by_type,
        "total_suggestions": sum(by_type.values()),
        "decided": decided,
        "accepted": accepted,
        "acceptance_rate": round(rate, 3),
        "streak_days": streak,
        "improvement_score": improvement_score,
    }


def get_history(days: int = 7) -> list[dict]:
    """Last N days of sessions with error counts."""
    cutoff = (datetime.now() - timedelta(days=days)).isoformat()
    with _lock:
        conn = _get_conn()
        rows = conn.execute(
            "SELECT id, date, word_count, error_count, accepted_count, mode, domain "
            "FROM sessions WHERE date >= ? ORDER BY date DESC",
            (cutoff,),
        ).fetchall()
    return [dict(r) for r in rows]


def get_top_mistakes(days: int = 7, limit: int = 3) -> list[dict]:
    """Most frequent error types over the past N days."""
    cutoff = (datetime.now() - timedelta(days=days)).isoformat()
    with _lock:
        conn = _get_conn()
        rows = conn.execute(
            "SELECT error_type, COUNT(*) AS n, "
            "MAX(original) AS example_original, MAX(suggestion) AS example_fix "
            "FROM suggestions WHERE timestamp >= ? "
            "GROUP BY error_type ORDER BY n DESC LIMIT ?",
            (cutoff, limit),
        ).fetchall()
    return [dict(r) for r in rows]


def get_weekly_summary() -> dict:
    cutoff = (datetime.now() - timedelta(days=7)).isoformat()
    with _lock:
        conn = _get_conn()
        fixed = conn.execute(
            "SELECT COUNT(*) FROM suggestions WHERE accepted = 1 AND timestamp >= ?",
            (cutoff,),
        ).fetchone()[0]
        top = conn.execute(
            "SELECT error_type FROM suggestions WHERE timestamp >= ? "
            "GROUP BY error_type ORDER BY COUNT(*) DESC LIMIT 1",
            (cutoff,),
        ).fetchone()
    return {
        "fixed_this_week": fixed,
        "most_common_error": top[0] if top else None,
    }


def get_daily_tip() -> dict:
    """Rotating tip: least-recently-shown first; stamps shown_date."""
    with _lock:
        conn = _get_conn()
        row = conn.execute(
            "SELECT id, tip, category FROM writing_tips "
            "ORDER BY shown_date IS NOT NULL, shown_date, id LIMIT 1"
        ).fetchone()
        if row is None:
            return {"tip": "Write every day — volume beats perfection.", "category": "habit"}
        conn.execute(
            "UPDATE writing_tips SET shown_date = ? WHERE id = ?",
            (datetime.now().isoformat(), row["id"]),
        )
        conn.commit()
    return {"tip": row["tip"], "category": row["category"]}
