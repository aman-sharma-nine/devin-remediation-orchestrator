"""SQLite persistence for the remediation orchestrator."""

import os
import sqlite3
import threading

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


def close_connection():
    global _conn
    with _lock:
        if _conn is not None:
            _conn.close()
            _conn = None
