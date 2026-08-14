"""SQLite persistence for the remediation orchestrator."""

import os
import sqlite3
import threading
from datetime import datetime, timezone

DATABASE_PATH = os.environ.get("DATABASE_PATH", "./orchestrator.db")

_lock = threading.Lock()

_conn = None


def init_db():
    global _conn
    with _lock:
        if _conn is None:
            db_dir = os.path.dirname(DATABASE_PATH)
            if db_dir:
                os.makedirs(db_dir, exist_ok=True)
            _conn = sqlite3.connect(DATABASE_PATH, check_same_thread=False)
            _conn.row_factory = sqlite3.Row
        _conn.execute(
            """
            CREATE TABLE IF NOT EXISTS deliveries (
                delivery_id TEXT PRIMARY KEY,
                received_at TEXT NOT NULL
            )
            """
        )
        _conn.execute(
            """
            CREATE TABLE IF NOT EXISTS sessions (
                issue_number INTEGER UNIQUE NOT NULL,
                session_id TEXT,
                session_url TEXT,
                advisory_id TEXT NOT NULL,
                package TEXT NOT NULL,
                outcome TEXT NOT NULL,
                phase TEXT,
                pr_url TEXT,
                head_sha TEXT,
                check_state TEXT NOT NULL DEFAULT 'absent',
                repair_attempts INTEGER NOT NULL DEFAULT 0,
                acus_consumed REAL,
                dispatched_at TEXT NOT NULL,
                verified_at TEXT,
                note TEXT
            )
            """
        )
        _conn.commit()


def record_delivery(delivery_id: str) -> bool:
    if not delivery_id:
        raise ValueError("delivery_id must be non-empty")

    received_at = datetime.now(timezone.utc).isoformat()

    with _lock:
        if _conn is None:
            raise RuntimeError("database connection has not been initialized")

        cursor = _conn.execute(
            """
            INSERT INTO deliveries (delivery_id, received_at) VALUES (?, ?)
            ON CONFLICT(delivery_id) DO NOTHING
            """,
            (delivery_id, received_at),
        )
        _conn.commit()
        return cursor.rowcount == 1


def claim_session(issue_number: int, advisory_id: str, package: str) -> bool:
    """Reserve an issue for a new Devin session.

    Returns True only if a new row was inserted, indicating no prior session
    exists for this issue. Returns False if a row already existed (conflict).
    This is the idempotency boundary at issue level.
    """
    if not isinstance(issue_number, int) or isinstance(issue_number, bool):
        raise ValueError("issue_number must be an integer")
    if not advisory_id or not advisory_id.strip():
        raise ValueError("advisory_id must be non-empty")
    if not package or not package.strip():
        raise ValueError("package must be non-empty")

    dispatched_at = datetime.now(timezone.utc).isoformat()

    with _lock:
        if _conn is None:
            raise RuntimeError("database connection has not been initialized")

        cursor = _conn.execute(
            """
            INSERT INTO sessions (
                issue_number, advisory_id, package, outcome, phase,
                check_state, repair_attempts, dispatched_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(issue_number) DO NOTHING
            """,
            (issue_number, advisory_id, package, "dispatched", "creating_session", "absent", 0, dispatched_at),
        )
        _conn.commit()
        return cursor.rowcount == 1


def complete_dispatch(issue_number: int, session_id: str, session_url: str) -> None:
    """Update a reserved session with Devin session details.

    Raises RuntimeError if no row was updated (e.g., claim_session was never called).
    """
    if not isinstance(issue_number, int) or isinstance(issue_number, bool):
        raise ValueError("issue_number must be an integer")
    if not session_id or not session_id.strip():
        raise ValueError("session_id must be non-empty")
    if not session_url or not session_url.strip():
        raise ValueError("session_url must be non-empty")

    with _lock:
        if _conn is None:
            raise RuntimeError("database connection has not been initialized")

        cursor = _conn.execute(
            """
            UPDATE sessions
            SET session_id = ?, session_url = ?, phase = ?
            WHERE issue_number = ?
            """,
            (session_id, session_url, "session_created", issue_number),
        )
        _conn.commit()
        if cursor.rowcount != 1:
            raise RuntimeError(f"no row found to update for issue {issue_number}")


def fail_dispatch(issue_number: int, note: str) -> None:
    """Mark a dispatch as failed with a safe error message."""
    if not isinstance(issue_number, int) or isinstance(issue_number, bool):
        raise ValueError("issue_number must be an integer")
    if not note or not note.strip():
        raise ValueError("note must be non-empty")

    with _lock:
        if _conn is None:
            raise RuntimeError("database connection has not been initialized")

        _conn.execute(
            """
            UPDATE sessions
            SET outcome = ?, phase = ?, note = ?
            WHERE issue_number = ?
            """,
            ("needs_human", "dispatch_failed", note, issue_number),
        )
        _conn.commit()


def get_session_by_issue(issue_number: int) -> dict | None:
    """Retrieve a session row by issue number.

    Returns the row as a plain dictionary, or None if not found.
    """
    if not isinstance(issue_number, int) or isinstance(issue_number, bool):
        raise ValueError("issue_number must be an integer")

    with _lock:
        if _conn is None:
            raise RuntimeError("database connection has not been initialized")

        cursor = _conn.execute(
            "SELECT * FROM sessions WHERE issue_number = ?",
            (issue_number,),
        )
        row = cursor.fetchone()
        if row is None:
            return None
        return dict(row)


def close_connection():
    global _conn
    with _lock:
        if _conn is not None:
            _conn.close()
            _conn = None
