"""Rehearsal - replay mail you already answered.

MailPilot drafts a reply to a message you have ALREADY answered, and shows its
draft next to what you actually sent. It is the only honest way to answer "is
this thing any good at sounding like me?" before you trust it with a live reply.

Hard properties of this module:
- **Read-only IMAP.** Every mailbox is opened with ``readonly=True`` (EXAMINE,
  not SELECT) and every fetch uses ``BODY.PEEK[...]``. Nothing here ever issues
  STORE, COPY, APPEND or EXPUNGE, so no \\Seen flag moves and no message is
  touched. A rehearsal must never change the mailbox it rehearses.
- **Rehearsal drafts are never queued for sending.** They land in the
  ``rehearsals`` table only. This module never writes to ``drafts`` - there is
  no code path from a rehearsal to the send choke-point.
- Credentials are parameters, never logged, never stored here. The integrator
  passes ``(address, app_password)`` per account (multi-inbox); this module has
  no opinion about where they came from.
"""
import email
import email.header
import email.utils
import imaplib
import re
import traceback
from contextlib import contextmanager

try:                       # inside the package, post-integration
    from . import db, drafter
    from .poller import NOREPLY_RE, extract_body  # noqa: F401  (NOREPLY_RE re-exported for radar)
except ImportError:        # standalone run from the scratchpad, pre-integration
    import os
    import sys
    sys.path.insert(0, os.environ.get("MAILPILOT_HOME", "/home/ed/mailpilot"))
    from mailpilot import db, drafter
    from mailpilot.poller import NOREPLY_RE, extract_body  # noqa: F401

# A copy of what db._SCHEMA ships, so the offline check below builds exactly the
# live table (see INTEGRATION.md).
REHEARSALS_DDL = """
CREATE TABLE IF NOT EXISTS rehearsals (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    from_address TEXT DEFAULT '',
    subject TEXT DEFAULT '',
    inbound_excerpt TEXT DEFAULT '',
    actual_reply TEXT DEFAULT '',
    draft TEXT DEFAULT '',
    created_at TEXT
);
"""

IMAP_HOST = "imap.gmail.com"
IMAP_PORT = 993
IMAP_TIMEOUT = 60

# Gmail localises these; \\Sent from LIST is authoritative, these are the net.
SENT_FALLBACKS = ("[Gmail]/Sent Mail", "Sent", "[Google Mail]/Sent Mail", "Sent Items")
ALL_MAIL_FALLBACKS = ("[Gmail]/All Mail", "[Google Mail]/All Mail", "INBOX")

MAX_REHEARSALS_PER_RUN = 10
EXCERPT_CHARS = 4000

_LIST_RE = re.compile(r'^\((?P<flags>[^)]*)\)\s+(?:"(?:[^"]*)"|NIL)\s+(?P<name>.+)$')
_SEQ_RE = re.compile(rb"^\s*(\d+)\s")


# --------------------------------------------------------------- IMAP plumbing
# (shared with radar.py - ship both files together, radar imports from here)

@contextmanager
def imap_session(address: str, app_password: str):
    """Logged-in IMAP connection. The password is a parameter and is never
    logged, echoed, or written anywhere by this module."""
    conn = imaplib.IMAP4_SSL(IMAP_HOST, IMAP_PORT, timeout=IMAP_TIMEOUT)
    try:
        conn.login(address, app_password)
        yield conn
    finally:
        try:
            conn.logout()
        except Exception:
            pass


def _quote_mailbox(name: str) -> str:
    """IMAP mailbox names with spaces/brackets must be quoted; imaplib does not
    do it for SELECT/EXAMINE, so we do."""
    if name.startswith('"') and name.endswith('"'):
        return name
    return '"' + name.replace("\\", "\\\\").replace('"', '\\"') + '"'


def select_readonly(conn, mailbox: str) -> bool:
    """EXAMINE the mailbox. readonly=True is load-bearing: it is what guarantees
    the server never marks anything \\Seen on our behalf."""
    try:
        typ, _ = conn.select(_quote_mailbox(mailbox), readonly=True)
        return typ == "OK"
    except imaplib.IMAP4.error:
        return False


def _list_entry_name(line: str) -> str:
    m = _LIST_RE.match(line.strip())
    if not m:
        return ""
    name = m.group("name").strip()
    if name.startswith('"') and name.endswith('"') and len(name) >= 2:
        name = name[1:-1].replace('\\"', '"').replace("\\\\", "\\")
    return name


def _special_use_folder(conn, flag: str) -> str:
    """Find a mailbox by RFC 6154 special-use flag (e.g. '\\Sent')."""
    try:
        typ, data = conn.list()
    except imaplib.IMAP4.error:
        return ""
    if typ != "OK":
        return ""
    for raw in data or []:
        line = raw.decode("utf-8", errors="replace") if isinstance(raw, bytes) else str(raw)
        if flag.lower() in line.lower().split(")")[0]:   # flags are before the first ')'
            name = _list_entry_name(line)
            if name:
                return name
    return ""


def find_sent_folder(conn) -> str:
    """Special-use \\Sent first, then the common names. '' if none selectable."""
    name = _special_use_folder(conn, "\\Sent")
    if name and select_readonly(conn, name):
        return name
    for cand in SENT_FALLBACKS:
        if select_readonly(conn, cand):
            return cand
    return ""


def find_all_mail_folder(conn) -> str:
    """Gmail's All Mail (special-use \\All), falling back to INBOX."""
    name = _special_use_folder(conn, "\\All")
    if name and select_readonly(conn, name):
        return name
    for cand in ALL_MAIL_FALLBACKS:
        if select_readonly(conn, cand):
            return cand
    return ""


def _decode_header(raw: str) -> str:
    """Mirror of poller.store_email's subject decoding, for any header."""
    if not raw:
        return ""
    try:
        parts = email.header.decode_header(raw)
    except Exception:
        return raw
    return "".join(
        p.decode(enc or "utf-8", errors="replace") if isinstance(p, bytes) else p
        for p, enc in parts
    ).strip()


def _header_date_iso(raw: str) -> str:
    """Date: header -> ISO-8601 TEXT *with offset* (house rule). Falls back to
    now() rather than emitting a naive stamp."""
    try:
        dt = email.utils.parsedate_to_datetime(raw)
    except (TypeError, ValueError):
        return db.now_iso()
    if dt is None:
        return db.now_iso()
    if dt.tzinfo is None:
        from datetime import timezone
        dt = dt.replace(tzinfo=timezone.utc).astimezone()
    return dt.isoformat(timespec="seconds")


def fetch_headers(conn, seqs, fields: str):
    """Batched header-only fetch. Returns [(seq:int, email.message.Message)].

    BODY.PEEK keeps every message unseen. The sequence number is recovered from
    the untagged response prefix so a batched fetch still maps back to a seq.
    """
    out = []
    if not seqs:
        return out
    spec = f"(BODY.PEEK[HEADER.FIELDS ({fields})])"
    for i in range(0, len(seqs), 200):
        chunk = seqs[i:i + 200]
        try:
            typ, data = conn.fetch(",".join(str(s) for s in chunk), spec)
        except imaplib.IMAP4.error:
            continue
        if typ != "OK":
            continue
        for item in data or []:
            if not isinstance(item, tuple) or len(item) < 2 or not item[1]:
                continue
            m = _SEQ_RE.match(item[0] if isinstance(item[0], bytes) else b"")
            seq = int(m.group(1)) if m else 0
            try:
                out.append((seq, email.message_from_bytes(item[1])))
            except Exception:
                continue
    return out


def _addr_of(raw: str):
    name, addr = email.utils.parseaddr(raw or "")
    return _decode_header(name), (addr or "").strip()


# ------------------------------------------------------------------ candidates

def list_sent_candidates(address: str, app_password: str, limit: int = 25) -> list[dict]:
    """Recent messages from the Sent folder that were replies to somebody.

    Returns newest-first: [{seq, to, subject, date, in_reply_to, message_id}].

    Only messages carrying an In-Reply-To are returned - a rehearsal replays a
    REPLY, and a cold outbound email has no inbound half to draft against. We
    therefore scan a wider window than `limit` and keep the first `limit` hits.

    `seq` is an IMAP sequence number: valid only against this mailbox as it
    stands now. `message_id` is returned alongside it so fetch_pair can verify
    it grabbed the message you actually picked (see fetch_pair's
    expect_message_id).
    """
    limit = max(1, int(limit))
    out: list[dict] = []
    with imap_session(address, app_password) as conn:
        sent = find_sent_folder(conn)
        if not sent:
            raise RuntimeError(
                "Could not find the Sent folder on this account (tried the \\Sent "
                "special-use flag and the common Gmail names)."
            )
        typ, data = conn.search(None, "ALL")
        if typ != "OK":
            raise RuntimeError(f"IMAP search of {sent} failed: {typ}")
        seqs = [int(n) for n in (data[0] or b"").split()]
        window = seqs[-min(len(seqs), max(limit * 6, 60)):]
        window.reverse()                                   # newest first
        pairs = fetch_headers(
            conn, window, "TO SUBJECT DATE MESSAGE-ID IN-REPLY-TO"
        )
        by_seq = {seq: msg for seq, msg in pairs}
        for seq in window:
            msg = by_seq.get(seq)
            if msg is None:
                continue
            in_reply_to = (msg.get("In-Reply-To") or "").strip()
            if not in_reply_to:
                continue
            _, to_addr = _addr_of(msg.get("To", ""))
            out.append({
                "seq": seq,
                "to": to_addr,
                "subject": _decode_header(msg.get("Subject", "")),
                "date": _header_date_iso(msg.get("Date", "")),
                "in_reply_to": in_reply_to,
                "message_id": (msg.get("Message-ID") or "").strip(),
            })
            if len(out) >= limit:
                break
    return out


def _full_message(conn, seq: int):
    """Whole message, still peeked (unseen)."""
    try:
        typ, data = conn.fetch(str(seq), "(BODY.PEEK[])")
    except imaplib.IMAP4.error:
        return None
    if typ != "OK":
        return None
    for item in data or []:
        if isinstance(item, tuple) and len(item) >= 2 and item[1]:
            try:
                return email.message_from_bytes(item[1])
            except Exception:
                return None
    return None


def _search_message_id(conn, message_id: str) -> list[int]:
    mid = (message_id or "").strip()
    if not mid:
        return []
    try:
        typ, data = conn.search(None, "HEADER", "Message-ID", f'"{mid}"')
    except imaplib.IMAP4.error:
        return []
    if typ != "OK":
        return []
    return [int(n) for n in (data[0] or b"").split()]


def fetch_pair(address: str, app_password: str, sent_seq: int,
               expect_message_id: str = "") -> dict | None:
    """The two halves of one rehearsal: the mail they sent you, and the reply
    you actually wrote.

    Returns {"inbound": {...pseudo email row...}, "actual_reply": text, "sent": {...}}
    or **None** when the original cannot be found - a missing original is an
    ordinary outcome (the thread was deleted, the reply started a thread we
    never received, All Mail is disabled), not an error. Nothing is written.

    expect_message_id (optional but recommended): the message_id that came back
    from list_sent_candidates for this seq. Sequence numbers shift when mail is
    expunged between the two calls; if the seq no longer points at that message
    we return None rather than rehearsing the wrong email.
    """
    with imap_session(address, app_password) as conn:
        sent_folder = find_sent_folder(conn)
        if not sent_folder:
            return None
        sent_msg = _full_message(conn, int(sent_seq))
        if sent_msg is None:
            return None
        if expect_message_id:
            got = (sent_msg.get("Message-ID") or "").strip()
            if got != expect_message_id.strip():
                return None                      # the mailbox moved under us
        in_reply_to = (sent_msg.get("In-Reply-To") or "").strip()
        if not in_reply_to:
            return None
        actual_reply = extract_body(sent_msg)
        sent_meta = {
            "subject": _decode_header(sent_msg.get("Subject", "")),
            "date": _header_date_iso(sent_msg.get("Date", "")),
            "message_id": (sent_msg.get("Message-ID") or "").strip(),
        }

        inbound_msg = None
        for mailbox in (find_all_mail_folder(conn) or "INBOX", "INBOX"):
            if not select_readonly(conn, mailbox):
                continue
            hits = _search_message_id(conn, in_reply_to)
            if hits:
                inbound_msg = _full_message(conn, hits[-1])
                if inbound_msg is not None:
                    break
        if inbound_msg is None:
            return None

        from_name, from_address = _addr_of(inbound_msg.get("From", ""))
        inbound = {
            "id": 0,                                  # pseudo row: not in `emails`
            "from_name": from_name,
            "from_address": from_address,
            "subject": _decode_header(inbound_msg.get("Subject", "")),
            "received_at": _header_date_iso(inbound_msg.get("Date", "")),
            "body_text": extract_body(inbound_msg),
            "message_id": (inbound_msg.get("Message-ID") or "").strip(),
        }
    return {"inbound": inbound, "actual_reply": actual_reply, "sent": sent_meta}


# ------------------------------------------------------------------- rehearsal

def _pseudo_row(inbound: dict) -> dict:
    """The shape drafter.build_user_prompt reads. id=0 marks it as not-an-email-
    row: no `emails` row exists and none is created."""
    return {
        "id": 0,
        "from_name": (inbound.get("from_name") or "").strip(),
        "from_address": (inbound.get("from_address") or "").strip(),
        "subject": (inbound.get("subject") or "").strip(),
        "received_at": inbound.get("received_at") or db.now_iso(),
        "body_text": (inbound.get("body_text") or "").strip(),
    }


def run_rehearsal(pairs, cfg: dict, generate_fn=None, db_file=None) -> dict:
    """Draft a reply for each pair and store it in `rehearsals`.

    pairs: iterable of fetch_pair results. At most MAX_REHEARSALS_PER_RUN (10)
    are processed - each one is a full model call, and this runs on a click.

    generate_fn(system_prompt, user_prompt, cfg) -> str, defaulting to
    drafter.generate. It exists so the offline check can prove the storage and
    capping behaviour without a network or an LLM.

    The draft produced here is DISPLAY-ONLY. This function does not touch the
    `drafts` table, so a rehearsal can never reach sender.send_reply().

    Returns {"created": n, "failed": n, "ids": [...], "skipped": n}.
    """
    cfg = dict(cfg or {})
    for key in ("signature_name", "gmail_address"):
        if not cfg.get(key):
            raise RuntimeError(f"Rehearsal needs cfg['{key}'] - open Settings and fill it in.")
    gen = generate_fn or drafter.generate

    # Same system prompt the live drafting path builds (voice profile + samples).
    samples = None if db_file is None else drafter._load_samples(db_file)
    system = drafter.build_system_prompt(cfg, samples)

    result = {"created": 0, "failed": 0, "ids": [], "skipped": 0}
    for pair in list(pairs or [])[:MAX_REHEARSALS_PER_RUN]:
        inbound = (pair or {}).get("inbound") or {}
        row = _pseudo_row(inbound)
        if not row["from_address"] or not row["body_text"]:
            result["skipped"] += 1
            continue
        # Deliberately WITHOUT drafter._sender_context: that pulls current thread
        # memory, which for an old message would include mail that arrived after
        # the reply being rehearsed. A rehearsal must not see the future.
        user = drafter.build_user_prompt(row)
        try:
            draft = (gen(system, user, cfg) or "").strip()
        except Exception as e:
            # Surface, don't swallow: this lands in the errors banner.
            db.record_error("rehearsal", f"Draft failed for {row['from_address']}: {e}",
                            traceback.format_exc(), db_file)
            result["failed"] += 1
            continue
        with db.conn(db_file) as c:
            cur = c.execute(
                "INSERT INTO rehearsals (from_address, subject, inbound_excerpt,"
                " actual_reply, draft, created_at) VALUES (?,?,?,?,?,?)",
                (row["from_address"], row["subject"], row["body_text"][:EXCERPT_CHARS],
                 (pair.get("actual_reply") or "")[:EXCERPT_CHARS], draft, db.now_iso()),
            )
            result["ids"].append(cur.lastrowid)
        result["created"] += 1
    return result


def list_rehearsals(limit: int = 50, db_file=None) -> list[dict]:
    with db.conn(db_file) as c:
        rows = c.execute(
            "SELECT id, from_address, subject, inbound_excerpt, actual_reply, draft,"
            " created_at FROM rehearsals ORDER BY id DESC LIMIT ?", (int(limit),),
        ).fetchall()
    return [dict(r) for r in rows]


def save_as_voice_sample(rehearsal_id: int, db_file=None) -> bool:
    """Copy the reply the user ACTUALLY wrote into voice_samples. The MailPilot
    draft is never promoted - that would teach the model from its own output."""
    with db.conn(db_file) as c:
        row = c.execute(
            "SELECT actual_reply FROM rehearsals WHERE id=?", (int(rehearsal_id),)
        ).fetchone()
        if row is None:
            return False
        body = (row["actual_reply"] or "").strip()
        if len(body) < 40:
            return False
        c.execute("INSERT INTO voice_samples (body, created_at) VALUES (?,?)",
                  (body[:20000], db.now_iso()))
    return True


# ----------------------------------------------------------------- offline check

if __name__ == "__main__":
    import tempfile
    from pathlib import Path

    tmp = Path(tempfile.mkdtemp(prefix="mailpilot-rehearsal-")) / "t.db"
    db.bootstrap(tmp)
    with db.conn(tmp) as c:
        c.executescript(REHEARSALS_DDL)

    CFG = {"gmail_address": "pilot@example.com", "signature_name": "Pilot", "mode": "api"}
    checks = []

    def check(name, cond):
        checks.append((name, bool(cond)))
        print(("  ok   " if cond else "  FAIL ") + name)

    def stub_generate(system, user, cfg):
        assert "Pilot" in system, "system prompt should carry the signature"
        assert "Draft a reply to this email" in user, "user prompt should come from build_user_prompt"
        return "Stub draft for: " + user.splitlines()[-1][:40]

    def pair(addr, body="Can we move Thursday's call to Friday morning?", reply="Friday 10am works."):
        return {
            "inbound": {"id": 0, "from_name": "Alice", "from_address": addr,
                        "subject": "Thursday", "received_at": db.now_iso(),
                        "body_text": body},
            "actual_reply": reply,
        }

    print("rehearsal.py offline checks (no network, no LLM)")

    r = run_rehearsal([pair("alice@example.com"), pair("bob@example.com")],
                      CFG, generate_fn=stub_generate, db_file=tmp)
    check("two pairs -> two rehearsal rows", r["created"] == 2 and len(r["ids"]) == 2)

    with db.conn(tmp) as c:
        n_drafts = c.execute("SELECT COUNT(*) FROM drafts").fetchone()[0]
        n_emails = c.execute("SELECT COUNT(*) FROM emails").fetchone()[0]
    check("rehearsal never queues a draft", n_drafts == 0)
    check("rehearsal never fabricates an emails row", n_emails == 0)

    stored = list_rehearsals(db_file=tmp)
    check("stored draft came from generate_fn", stored[0]["draft"].startswith("Stub draft for:"))
    check("stored actual_reply is the real sent text", stored[0]["actual_reply"] == "Friday 10am works.")

    r = run_rehearsal([pair(f"x{i}@example.com") for i in range(12)],
                      CFG, generate_fn=stub_generate, db_file=tmp)
    check("run is capped at 10 pairs", r["created"] == 10)

    def boom(system, user, cfg):
        raise RuntimeError("model unavailable")

    r = run_rehearsal([pair("carol@example.com")], CFG, generate_fn=boom, db_file=tmp)
    with db.conn(tmp) as c:
        errs = c.execute("SELECT source, message FROM errors").fetchall()
    check("a generate failure is counted, not raised", r["failed"] == 1 and r["created"] == 0)
    check("a generate failure SURFACES in errors", any(e["source"] == "rehearsal" for e in errs))

    r = run_rehearsal([{"inbound": {"from_address": "", "body_text": ""}, "actual_reply": ""}],
                      CFG, generate_fn=stub_generate, db_file=tmp)
    check("an empty pair is skipped, not drafted", r["skipped"] == 1 and r["created"] == 0)

    rid = stored[0]["id"]
    check("save_as_voice_sample refuses a too-short reply", save_as_voice_sample(rid, db_file=tmp) is False)
    with db.conn(tmp) as c:
        c.execute("UPDATE rehearsals SET actual_reply=? WHERE id=?",
                  ("Friday 10am works for me - I'll send an invite over shortly. Thanks for the nudge.", rid))
    check("save_as_voice_sample copies a real reply", save_as_voice_sample(rid, db_file=tmp) is True)
    with db.conn(tmp) as c:
        vs = c.execute("SELECT body FROM voice_samples").fetchall()
    check("voice_samples got the USER's reply, not the draft",
          len(vs) == 1 and vs[0]["body"].startswith("Friday 10am works for me"))

    # Header/LIST parsing, exercised without a server.
    check("LIST line -> mailbox name",
          _list_entry_name(r'(\HasNoChildren \Sent) "/" "[Gmail]/Sent Mail"') == "[Gmail]/Sent Mail")
    check("unquoted LIST name", _list_entry_name(r'(\HasNoChildren) "." INBOX.Sent') == "INBOX.Sent")
    check("mailbox quoting", _quote_mailbox("[Gmail]/All Mail") == '"[Gmail]/All Mail"')
    check("already-quoted mailbox left alone", _quote_mailbox('"Sent"') == '"Sent"')
    check("MIME subject decoded",
          _decode_header("=?utf-8?q?Caf=C3=A9_meeting?=") == "Café meeting")
    check("Date header keeps an offset",
          "+" in _header_date_iso("Tue, 12 Aug 2025 09:13:00 +0200"))
    check("unparseable Date falls back to an offset-bearing now",
          _header_date_iso("not a date")[-6] in "+-")

    bad = sum(1 for _, ok in checks if not ok)
    print(f"\n{len(checks) - bad}/{len(checks)} checks passed")
    raise SystemExit(1 if bad else 0)
