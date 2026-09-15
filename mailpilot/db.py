"""SQLite storage. WAL mode so the poller thread and web handlers can share the
file. Every datetime column is TEXT, ISO-8601, with timezone offset.
"""
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone

from . import paths

_SCHEMA = """
CREATE TABLE IF NOT EXISTS emails (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    message_id TEXT UNIQUE NOT NULL,
    thread_references TEXT DEFAULT '',
    from_address TEXT NOT NULL,
    from_name TEXT DEFAULT '',
    subject TEXT DEFAULT '',
    body_text TEXT DEFAULT '',
    received_at TEXT,
    processed_at TEXT,
    status TEXT NOT NULL DEFAULT 'drafted',   -- 'drafted' | 'skipped'
    skip_reason TEXT DEFAULT '',
    draft_attempts INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS drafts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    email_id INTEGER NOT NULL REFERENCES emails(id),
    body TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL DEFAULT 'queued',    -- queued|approved|sent|simulated|discarded|blocked
    block_reason TEXT DEFAULT '',
    created_at TEXT,
    updated_at TEXT,
    sent_at TEXT
);
CREATE TABLE IF NOT EXISTS errors (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    source TEXT NOT NULL,
    message TEXT NOT NULL,
    traceback TEXT DEFAULT '',
    created_at TEXT,
    acknowledged INTEGER NOT NULL DEFAULT 0
);
"""


def now_iso() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


@contextmanager
def conn(db_file=None):
    c = sqlite3.connect(str(db_file or paths.db_path()), timeout=10)
    c.row_factory = sqlite3.Row
    c.execute("PRAGMA journal_mode=WAL")
    c.execute("PRAGMA busy_timeout=5000")
    try:
        yield c
        c.commit()
    except Exception:
        c.rollback()
        raise
    finally:
        c.close()


def bootstrap(db_file=None) -> None:
    with conn(db_file) as c:
        c.executescript(_SCHEMA)


def record_error(source: str, message: str, tb: str = "", db_file=None) -> None:
    """Failures must SURFACE: this table renders as a banner in the UI."""
    try:
        with conn(db_file) as c:
            c.execute(
                "INSERT INTO errors (source, message, traceback, created_at) VALUES (?,?,?,?)",
                (source, str(message)[:500], tb[:8000], now_iso()),
            )
    except Exception:
        pass  # an error about an error must never crash the caller
