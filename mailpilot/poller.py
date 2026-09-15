"""The inbox watcher.

Rules that keep it safe and predictable:
- Dedup on Message-ID; a message already in the DB is ignored entirely.
- Bulk/no-reply/own mail is stored as 'skipped' with a named reason.
- \\Seen is set on the server only AFTER the local row (and, for real mail, its
  draft) is committed - a crash mid-cycle leaves the message unseen so the next
  cycle retries it.
- Any exception inside a cycle writes an errors row and the loop continues.
"""
import email
import email.utils
import imaplib
import re
import threading
import traceback
from html.parser import HTMLParser

from . import config, db, drafter

NOREPLY_RE = re.compile(r"(no-?reply|donotreply|mailer-daemon|postmaster)", re.I)
MAX_BODY = 20_000
MAX_DRAFT_ATTEMPTS = 3


class _HTMLToText(HTMLParser):
    def __init__(self):
        super().__init__()
        self.parts = []
        self._skip = 0

    def handle_starttag(self, tag, attrs):
        if tag in ("script", "style"):
            self._skip += 1
        elif tag in ("br", "p", "div", "tr", "li"):
            self.parts.append("\n")

    def handle_endtag(self, tag):
        if tag in ("script", "style") and self._skip:
            self._skip -= 1

    def handle_data(self, data):
        if not self._skip:
            self.parts.append(data)


def html_to_text(html: str) -> str:
    p = _HTMLToText()
    try:
        p.feed(html)
    except Exception:
        return html
    return re.sub(r"\n{3,}", "\n\n", "".join(p.parts)).strip()


def extract_body(msg) -> str:
    plain, html = None, None
    if msg.is_multipart():
        for part in msg.walk():
            ctype = part.get_content_type()
            if part.get("Content-Disposition", "").startswith("attachment"):
                continue
            try:
                payload = part.get_payload(decode=True)
            except Exception:
                continue
            if payload is None:
                continue
            charset = part.get_content_charset() or "utf-8"
            text = payload.decode(charset, errors="replace")
            if ctype == "text/plain" and plain is None:
                plain = text
            elif ctype == "text/html" and html is None:
                html = text
    else:
        try:
            payload = msg.get_payload(decode=True)
            if payload is not None:
                charset = msg.get_content_charset() or "utf-8"
                text = payload.decode(charset, errors="replace")
                if msg.get_content_type() == "text/html":
                    html = text
                else:
                    plain = text
        except Exception:
            pass
    body = plain if plain is not None else (html_to_text(html) if html else "")
    return body.strip()[:MAX_BODY]


def _addr_matches(address: str, patterns: list[str]) -> bool:
    address = (address or "").lower().strip()
    domain = address.split("@")[-1] if "@" in address else ""
    for p in patterns:
        p = (p or "").lower().strip()
        if not p:
            continue
        if p == address or p == domain or (p.startswith("@") and p[1:] == domain):
            return True
    return False


def classify(msg, from_address: str, cfg: dict) -> str:
    """Return '' to draft, or a named skip reason."""
    if from_address.lower() == (cfg.get("gmail_address") or "").lower():
        return "own_address"
    if NOREPLY_RE.search(from_address):
        return "no_reply_sender"
    if msg.get("List-Unsubscribe") or (msg.get("Precedence", "").lower() in ("bulk", "list")):
        return "bulk_mail"
    if _addr_matches(from_address, cfg.get("ignore_senders") or []):
        return "ignored_sender"
    only = cfg.get("only_senders") or []
    if only and not _addr_matches(from_address, only):
        return "not_on_allowlist"
    return ""


def store_email(msg, cfg: dict, db_file=None):
    """Insert the email (dedup on Message-ID). Returns (email_id|None, status, skip_reason)."""
    message_id = (msg.get("Message-ID") or "").strip()
    if not message_id:
        message_id = f"<missing-{hash(msg.as_bytes()[:2000])}@mailpilot>"
    from_name, from_address = email.utils.parseaddr(msg.get("From", ""))
    subject_parts = email.header.decode_header(msg.get("Subject", "") or "")
    subject = "".join(
        p.decode(enc or "utf-8", errors="replace") if isinstance(p, bytes) else p
        for p, enc in subject_parts
    )
    date_hdr = msg.get("Date", "")
    try:
        received_at = email.utils.parsedate_to_datetime(date_hdr).isoformat(timespec="seconds")
    except Exception:
        received_at = db.now_iso()
    skip_reason = classify(msg, from_address, cfg)
    status = "skipped" if skip_reason else "drafted"
    refs = (msg.get("References") or "").strip()
    with db.conn(db_file) as c:
        dup = c.execute("SELECT id FROM emails WHERE message_id=?", (message_id,)).fetchone()
        if dup:
            return None, "duplicate", ""
        cur = c.execute(
            "INSERT INTO emails (message_id, thread_references, from_address, from_name,"
            " subject, body_text, received_at, processed_at, status, skip_reason)"
            " VALUES (?,?,?,?,?,?,?,?,?,?)",
            (message_id, refs, from_address, from_name, subject,
             extract_body(msg), received_at, db.now_iso(), status, skip_reason),
        )
        return cur.lastrowid, status, skip_reason


def draft_for_email(email_id: int, cfg: dict, db_file=None) -> bool:
    """Create the queued draft. On failure: errors row, bump attempts; after
    MAX_DRAFT_ATTEMPTS mark skipped(draft_failed) so it's visible, not gone."""
    with db.conn(db_file) as c:
        row = c.execute("SELECT * FROM emails WHERE id=?", (email_id,)).fetchone()
    if row is None or row["status"] != "drafted":
        return False
    try:
        body = drafter.draft_reply(row, cfg)
    except Exception as e:
        db.record_error("drafter", str(e), traceback.format_exc(), db_file)
        with db.conn(db_file) as c:
            attempts = row["draft_attempts"] + 1
            if attempts >= MAX_DRAFT_ATTEMPTS:
                c.execute(
                    "UPDATE emails SET draft_attempts=?, status='skipped',"
                    " skip_reason='draft_failed' WHERE id=?",
                    (attempts, email_id),
                )
            else:
                c.execute("UPDATE emails SET draft_attempts=? WHERE id=?", (attempts, email_id))
        return False
    now = db.now_iso()
    with db.conn(db_file) as c:
        c.execute(
            "INSERT INTO drafts (email_id, body, status, created_at, updated_at)"
            " VALUES (?,?, 'queued', ?, ?)",
            (email_id, body, now, now),
        )
    return True


def poll_once(cfg: dict = None, db_file=None) -> dict:
    """One IMAP cycle. Returns counters for the UI/log."""
    cfg = cfg or config.load()
    stats = {"new": 0, "skipped": 0, "drafted": 0, "retried": 0}
    password = config.get_secret("gmail_app_password")
    if not (cfg.get("gmail_address") and password):
        return stats

    # Retry earlier draft failures first (no IMAP needed)
    with db.conn(db_file) as c:
        pending = c.execute(
            "SELECT e.id FROM emails e LEFT JOIN drafts d ON d.email_id=e.id"
            " WHERE e.status='drafted' AND d.id IS NULL"
        ).fetchall()
    for row in pending:
        stats["retried"] += 1
        if draft_for_email(row["id"], cfg, db_file):
            stats["drafted"] += 1

    imap = imaplib.IMAP4_SSL("imap.gmail.com", 993)
    try:
        imap.login(cfg["gmail_address"], password)
        imap.select("INBOX")
        typ, data = imap.search(None, "UNSEEN")
        if typ != "OK":
            raise RuntimeError(f"IMAP search failed: {typ}")
        for num in data[0].split():
            # BODY.PEEK keeps the message unseen until we've fully processed it
            typ, msg_data = imap.fetch(num, "(BODY.PEEK[])")
            if typ != "OK" or not msg_data or msg_data[0] is None:
                continue
            msg = email.message_from_bytes(msg_data[0][1])
            email_id, status, _reason = store_email(msg, cfg, db_file)
            if status == "duplicate":
                imap.store(num, "+FLAGS", "\\Seen")
                continue
            stats["new"] += 1
            if status == "skipped":
                stats["skipped"] += 1
                imap.store(num, "+FLAGS", "\\Seen")
                continue
            drafted = draft_for_email(email_id, cfg, db_file)
            if drafted:
                stats["drafted"] += 1
            # Mark seen even if drafting failed: the email row exists locally and
            # the retry path above owns it now - re-reading it would just dup.
            imap.store(num, "+FLAGS", "\\Seen")
    finally:
        try:
            imap.logout()
        except Exception:
            pass
    return stats


def validate_gmail(address: str, app_password: str) -> tuple[bool, str]:
    """Live IMAP + SMTP login test for the setup wizard."""
    import smtplib
    try:
        imap = imaplib.IMAP4_SSL("imap.gmail.com", 993)
        imap.login(address, app_password)
        imap.logout()
    except imaplib.IMAP4.error:
        return False, ("Gmail rejected the sign-in for reading mail. Check the address, and that "
                       "this is a 16-character app password (not your normal password).")
    except OSError:
        return False, "Could not reach Gmail (network error)."
    try:
        smtp = smtplib.SMTP_SSL("smtp.gmail.com", 465, timeout=20)
        smtp.login(address, app_password)
        smtp.quit()
    except smtplib.SMTPAuthenticationError:
        return False, "Reading works, but Gmail rejected the sign-in for sending. Regenerate the app password."
    except OSError:
        return False, "Could not reach Gmail's sending server (network error)."
    return True, "Gmail connection works - reading and sending both verified."


class PollLoop(threading.Thread):
    """Background watcher thread. Never dies on a cycle error."""

    def __init__(self):
        super().__init__(daemon=True, name="mailpilot-poller")
        self._stop = threading.Event()
        self.wake = threading.Event()   # set to trigger an immediate cycle
        self.last_result = {}
        self.last_run = ""

    def stop(self):
        self._stop.set()
        self.wake.set()

    def run(self):
        while not self._stop.is_set():
            cfg = config.load()
            if cfg.get("configured"):
                try:
                    self.last_result = poll_once(cfg)
                except Exception as e:
                    db.record_error("poller", str(e), traceback.format_exc())
                    self.last_result = {"error": str(e)[:200]}
                self.last_run = db.now_iso()
            interval = max(60, int(cfg.get("poll_interval") or 300))
            self.wake.wait(timeout=interval)
            self.wake.clear()
