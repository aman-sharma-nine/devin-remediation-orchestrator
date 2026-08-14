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


def list_pollable_sessions() -> list[dict]:
    """Return rows that still need Devin polling.

    A row is pollable when it has a non-empty session_id and its outcome
    is "dispatched", "working", or "repairing" (not yet awaiting_ci,
    blocked, needs_human, or verified). "repairing" rows are included so
    the poller can detect when Devin pushes a new commit during a repair.
    """
    with _lock:
        if _conn is None:
            raise RuntimeError("database connection has not been initialized")

        cursor = _conn.execute(
            """
            SELECT * FROM sessions
            WHERE session_id IS NOT NULL AND session_id != ''
              AND outcome IN ('dispatched', 'working', 'repairing')
            """
        )
        return [dict(row) for row in cursor.fetchall()]


def update_polling_state(
    issue_number: int,
    outcome: str,
    phase: str,
    acus_consumed: float | None = None,
) -> bool:
    """Update outcome/phase/acus_consumed for a polled session.

    Writes and commits only if at least one value actually changed.
    Returns True if a write occurred, False if the row already matched.

    Raises RuntimeError if no row exists for the issue.
    """
    if not isinstance(issue_number, int) or isinstance(issue_number, bool):
        raise ValueError("issue_number must be an integer")
    if not outcome or not outcome.strip():
        raise ValueError("outcome must be non-empty")
    if not phase or not phase.strip():
        raise ValueError("phase must be non-empty")

    with _lock:
        if _conn is None:
            raise RuntimeError("database connection has not been initialized")

        cursor = _conn.execute(
            "SELECT outcome, phase, acus_consumed FROM sessions WHERE issue_number = ?",
            (issue_number,),
        )
        row = cursor.fetchone()
        if row is None:
            raise RuntimeError(f"no row found for issue {issue_number}")

        if row["outcome"] == outcome and row["phase"] == phase and row["acus_consumed"] == acus_consumed:
            return False

        _conn.execute(
            "UPDATE sessions SET outcome = ?, phase = ?, acus_consumed = ? WHERE issue_number = ?",
            (outcome, phase, acus_consumed, issue_number),
        )
        _conn.commit()
        return True


def record_pr_discovered(
    issue_number: int,
    pr_url: str,
    head_sha: str,
    acus_consumed: float | None = None,
) -> bool:
    """Record PR discovery: store pr_url, head_sha, set outcome=awaiting_ci,
    phase=pr_discovered.

    Writes and commits only if at least one value actually changed.
    Returns True if a write occurred, False if the row already matched.

    Raises RuntimeError if no row exists for the issue.
    """
    if not isinstance(issue_number, int) or isinstance(issue_number, bool):
        raise ValueError("issue_number must be an integer")
    if not pr_url or not pr_url.strip():
        raise ValueError("pr_url must be non-empty")
    if not head_sha or not head_sha.strip():
        raise ValueError("head_sha must be non-empty")

    with _lock:
        if _conn is None:
            raise RuntimeError("database connection has not been initialized")

        cursor = _conn.execute(
            "SELECT pr_url, head_sha, outcome, phase, acus_consumed FROM sessions WHERE issue_number = ?",
            (issue_number,),
        )
        row = cursor.fetchone()
        if row is None:
            raise RuntimeError(f"no row found for issue {issue_number}")

        if (
            row["pr_url"] == pr_url
            and row["head_sha"] == head_sha
            and row["outcome"] == "awaiting_ci"
            and row["phase"] == "pr_discovered"
            and row["acus_consumed"] == acus_consumed
        ):
            return False

        _conn.execute(
            """
            UPDATE sessions
            SET pr_url = ?, head_sha = ?, outcome = ?, phase = ?, acus_consumed = ?
            WHERE issue_number = ?
            """,
            (pr_url, head_sha, "awaiting_ci", "pr_discovered", acus_consumed, issue_number),
        )
        _conn.commit()
        return True


def get_session_by_head_sha(head_sha: str) -> dict | None:
    """Retrieve a session row by its stored head_sha, restricted to rows
    currently awaiting CI (outcome=awaiting_ci).

    This scoping matters once a repair starts: the failed SHA stays stored
    on the row while outcome=repairing (see start_repair), so a duplicate
    or stale workflow_run event for that same failed SHA must not match
    and re-trigger handling while the row is mid-repair.

    Returns the row as a plain dictionary, or None if not found.
    """
    if not head_sha or not head_sha.strip():
        raise ValueError("head_sha must be non-empty")

    with _lock:
        if _conn is None:
            raise RuntimeError("database connection has not been initialized")

        cursor = _conn.execute(
            "SELECT * FROM sessions WHERE head_sha = ? AND outcome = 'awaiting_ci'",
            (head_sha,),
        )
        row = cursor.fetchone()
        if row is None:
            return None
        return dict(row)


def mark_verified(issue_number: int) -> bool:
    """Mark a session verified: outcome=verified, phase=verified,
    check_state=green, verified_at=now.

    No-op (returns False) if the row is already verified, so a duplicate
    workflow_run delivery does not overwrite verified_at.

    Raises RuntimeError if no row exists for the issue.
    """
    if not isinstance(issue_number, int) or isinstance(issue_number, bool):
        raise ValueError("issue_number must be an integer")

    verified_at = datetime.now(timezone.utc).isoformat()

    with _lock:
        if _conn is None:
            raise RuntimeError("database connection has not been initialized")

        cursor = _conn.execute(
            "SELECT outcome FROM sessions WHERE issue_number = ?",
            (issue_number,),
        )
        row = cursor.fetchone()
        if row is None:
            raise RuntimeError(f"no row found for issue {issue_number}")

        if row["outcome"] == "verified":
            return False

        _conn.execute(
            """
            UPDATE sessions
            SET outcome = 'verified', phase = 'verified', check_state = 'green', verified_at = ?
            WHERE issue_number = ?
            """,
            (verified_at, issue_number),
        )
        _conn.commit()
        return True


def mark_needs_human(issue_number: int, phase: str, note: str, check_state: str) -> bool:
    """Mark a session needs_human with a specific phase/note/check_state.

    Writes and commits only if at least one value actually changed.
    Returns True if a write occurred, False if the row already matched.

    Raises RuntimeError if no row exists for the issue.
    """
    if not isinstance(issue_number, int) or isinstance(issue_number, bool):
        raise ValueError("issue_number must be an integer")
    if not phase or not phase.strip():
        raise ValueError("phase must be non-empty")
    if not note or not note.strip():
        raise ValueError("note must be non-empty")
    if not check_state or not check_state.strip():
        raise ValueError("check_state must be non-empty")

    with _lock:
        if _conn is None:
            raise RuntimeError("database connection has not been initialized")

        cursor = _conn.execute(
            "SELECT outcome, phase, note, check_state FROM sessions WHERE issue_number = ?",
            (issue_number,),
        )
        row = cursor.fetchone()
        if row is None:
            raise RuntimeError(f"no row found for issue {issue_number}")

        if (
            row["outcome"] == "needs_human"
            and row["phase"] == phase
            and row["note"] == note
            and row["check_state"] == check_state
        ):
            return False

        _conn.execute(
            """
            UPDATE sessions
            SET outcome = 'needs_human', phase = ?, note = ?, check_state = ?
            WHERE issue_number = ?
            """,
            (phase, note, check_state, issue_number),
        )
        _conn.commit()
        return True


def start_repair(issue_number: int) -> bool:
    """Atomically start the single allowed repair attempt.

    Sets repair_attempts=1, outcome=repairing, phase=repairing,
    check_state=failed. head_sha is intentionally KEPT (not cleared) -
    it holds the failed commit's SHA so the Step 9 poller can compare it
    against GitHub's live PR head SHA and detect when Devin pushes a new
    commit (see record_repair_pushed).

    The WHERE repair_attempts = 0 guard makes this atomic against a
    duplicate workflow_run delivery: only the first caller succeeds.

    Returns True if repair was started, False if a repair was already
    in progress or already attempted.
    """
    if not isinstance(issue_number, int) or isinstance(issue_number, bool):
        raise ValueError("issue_number must be an integer")

    with _lock:
        if _conn is None:
            raise RuntimeError("database connection has not been initialized")

        cursor = _conn.execute(
            """
            UPDATE sessions
            SET repair_attempts = 1, outcome = 'repairing', phase = 'repairing',
                check_state = 'failed'
            WHERE issue_number = ? AND repair_attempts = 0
            """,
            (issue_number,),
        )
        _conn.commit()
        return cursor.rowcount == 1


def record_repair_pushed(issue_number: int, head_sha: str) -> bool:
    """Record that Devin pushed a new commit during repair.

    Updates head_sha to the new SHA and sets outcome=awaiting_ci,
    phase=repair_pushed, check_state=absent, so the row re-enters the
    normal CI-verification path via the new commit.

    Writes and commits only if at least one value actually changed.
    Returns True if a write occurred, False if the row already matched.

    Raises RuntimeError if no row exists for the issue.
    """
    if not isinstance(issue_number, int) or isinstance(issue_number, bool):
        raise ValueError("issue_number must be an integer")
    if not head_sha or not head_sha.strip():
        raise ValueError("head_sha must be non-empty")

    with _lock:
        if _conn is None:
            raise RuntimeError("database connection has not been initialized")

        cursor = _conn.execute(
            "SELECT head_sha, outcome, phase, check_state FROM sessions WHERE issue_number = ?",
            (issue_number,),
        )
        row = cursor.fetchone()
        if row is None:
            raise RuntimeError(f"no row found for issue {issue_number}")

        if (
            row["head_sha"] == head_sha
            and row["outcome"] == "awaiting_ci"
            and row["phase"] == "repair_pushed"
            and row["check_state"] == "absent"
        ):
            return False

        _conn.execute(
            """
            UPDATE sessions
            SET head_sha = ?, outcome = 'awaiting_ci', phase = 'repair_pushed', check_state = 'absent'
            WHERE issue_number = ?
            """,
            (head_sha, issue_number),
        )
        _conn.commit()
        return True


def get_dashboard_data() -> dict:
    """Return read-only dashboard data: three totals and all session rows,
    newest first by dispatched_at.

    Read-only - does not write to the database.
    """
    with _lock:
        if _conn is None:
            raise RuntimeError("database connection has not been initialized")

        dispatched = _conn.execute(
            "SELECT COUNT(*) FROM sessions WHERE session_id IS NOT NULL AND session_id != ''"
        ).fetchone()[0]

        prs_opened = _conn.execute(
            "SELECT COUNT(*) FROM sessions WHERE pr_url IS NOT NULL AND pr_url != ''"
        ).fetchone()[0]

        ci_verified = _conn.execute(
            "SELECT COUNT(*) FROM sessions WHERE outcome = 'verified' AND check_state = 'green'"
        ).fetchone()[0]

        cursor = _conn.execute("SELECT * FROM sessions ORDER BY dispatched_at DESC")
        sessions = [dict(row) for row in cursor.fetchall()]

    return {
        "totals": {
            "dispatched": dispatched,
            "prs_opened": prs_opened,
            "ci_verified": ci_verified,
        },
        "sessions": sessions,
    }


def close_connection():
    global _conn
    with _lock:
        if _conn is not None:
            _conn.close()
            _conn = None
