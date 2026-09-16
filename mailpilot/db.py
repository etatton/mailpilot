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
CREATE TABLE IF NOT EXISTS voice_samples (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    body TEXT NOT NULL,
    created_at TEXT
);
CREATE TABLE IF NOT EXISTS contacts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    address TEXT UNIQUE NOT NULL,      -- lowercased email address or bare domain
    name TEXT DEFAULT '',
    notes TEXT DEFAULT '',
    rule TEXT NOT NULL DEFAULT 'normal',  -- normal | vip | always_draft | auto_skip
    created_at TEXT
);
CREATE TABLE IF NOT EXISTS accounts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    address TEXT UNIQUE NOT NULL,
    label TEXT DEFAULT '',
    created_at TEXT
);
CREATE TABLE IF NOT EXISTS kv (
    key TEXT PRIMARY KEY,
    value TEXT
);
CREATE TABLE IF NOT EXISTS correspondence_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    address TEXT NOT NULL,
    direction TEXT NOT NULL,           -- 'in' | 'out'
    date TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_correspondence_address ON correspondence_log(address);
CREATE TABLE IF NOT EXISTS rehearsals (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    from_address TEXT DEFAULT '',
    subject TEXT DEFAULT '',
    inbound_excerpt TEXT DEFAULT '',
    actual_reply TEXT DEFAULT '',
    draft TEXT DEFAULT '',
    created_at TEXT
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


# Additions to tables that shipped in earlier versions (idempotent, guarded by
# PRAGMA table_info so existing installs upgrade in place).
_COLUMN_ADDITIONS = [
    ("drafts", "notified", "INTEGER NOT NULL DEFAULT 0"),
    ("drafts", "kind", "TEXT NOT NULL DEFAULT 'reply'"),   # reply | followup
    ("emails", "vip", "INTEGER NOT NULL DEFAULT 0"),
    # Multi-inbox: every pre-existing email belongs to the original (primary)
    # account, which the migration below guarantees is id 1.
    ("emails", "account_id", "INTEGER NOT NULL DEFAULT 1"),
    ("emails", "attachments_json", "TEXT NOT NULL DEFAULT ''"),
    ("drafts", "snoozed_until", "TEXT NOT NULL DEFAULT ''"),
    ("drafts", "meta_json", "TEXT NOT NULL DEFAULT ''"),   # negotiation stances / parley state
]


def _migrate_primary_account(c) -> None:
    """Seed accounts from the single-inbox config, once.

    Runs only when accounts is empty, so a user who has since added or removed
    inboxes is never second-guessed. Existing emails keep account_id 1, which is
    what the row inserted here becomes.
    """
    have = c.execute("SELECT COUNT(*) FROM accounts").fetchone()[0]
    if have:
        return
    from . import config  # local import: config imports paths only, db does too
    address = (config.load().get("gmail_address") or "").strip()
    if not address:
        return
    c.execute(
        "INSERT INTO accounts (id, address, label, created_at) VALUES (1,?,?,?)",
        (address, "Primary", now_iso()),
    )


def ensure_primary_account(db_file=None) -> None:
    """Self-heal: a DB with no accounts row but a configured address gets one."""
    with conn(db_file) as c:
        _migrate_primary_account(c)


def bootstrap(db_file=None) -> None:
    with conn(db_file) as c:
        c.executescript(_SCHEMA)
        for table, col, decl in _COLUMN_ADDITIONS:
            have = {r[1] for r in c.execute(f"PRAGMA table_info({table})")}
            if col not in have:
                c.execute(f"ALTER TABLE {table} ADD COLUMN {col} {decl}")
        _migrate_primary_account(c)


def kv_get(key: str, db_file=None) -> str:
    with conn(db_file) as c:
        row = c.execute("SELECT value FROM kv WHERE key=?", (key,)).fetchone()
    return row["value"] if row else ""


def kv_set(key: str, value: str, db_file=None) -> None:
    with conn(db_file) as c:
        c.execute(
            "INSERT INTO kv (key, value) VALUES (?,?)"
            " ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (key, value),
        )


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
