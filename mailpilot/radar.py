"""Relationship Radar - people you used to talk to.

Some correspondence stops on purpose. Some just stops. Radar looks for the
second kind: an address you exchanged mail with regularly for months, and then
nothing, in either direction, for two months.

Hard properties:
- **Read-only IMAP.** The backfill EXAMINEs mailboxes (readonly=True) and fetches
  only header fields with BODY.PEEK. No flag is set, nothing is written to the
  server, no message is marked seen.
- **No LLM.** compute_drift is arithmetic over local rows. The only model call in
  this feature is the reconnection draft, which the integrator triggers from the
  server and which goes through the normal drafts queue and send guards.
- Credentials are parameters (multi-inbox), never logged.

Cadence detection is deliberately blunt. The failure mode to avoid is not
"missed a lapsed contact", it is "cried wolf about a mailing list", which is why
no-reply/bulk senders are dropped at insert time AND filtered again at read time.
"""
import email
import email.utils
import json
import re
import traceback
from datetime import datetime, timedelta, timezone

try:                       # inside the package, post-integration
    from . import db
    from .poller import NOREPLY_RE, _parse_dt
    from .rehearsal import (fetch_headers, find_all_mail_folder, find_sent_folder,
                            imap_session, select_readonly)
except ImportError:        # standalone run from the scratchpad, pre-integration
    import os
    import sys
    sys.path.insert(0, os.environ.get("MAILPILOT_HOME", "/home/ed/mailpilot"))
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    from mailpilot import db
    from mailpilot.poller import NOREPLY_RE, _parse_dt
    from rehearsal import (fetch_headers, find_all_mail_folder, find_sent_folder,
                           imap_session, select_readonly)

# The table this module needs - a copy of what db._SCHEMA ships, so the offline
# check below runs against exactly the live schema. Dedupe is enforced in
# _insert_rows (WHERE NOT EXISTS), NOT by a unique index; see INTEGRATION.md.
CORRESPONDENCE_LOG_DDL = """
CREATE TABLE IF NOT EXISTS correspondence_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    address TEXT NOT NULL,
    direction TEXT NOT NULL,           -- 'in' | 'out'
    date TEXT NOT NULL                 -- ISO-8601 with offset
);
CREATE INDEX IF NOT EXISTS idx_correspondence_address ON correspondence_log(address);
"""

# --- drift thresholds (all of these are the whole policy) ---------------------
BASELINE_START_DAYS = 365   # window opens 12 months ago
BASELINE_END_DAYS = 60      # ...and closes 2 months ago
QUIET_DAYS = 60             # silence, either direction, that makes it a finding
MIN_ACTIVE_MONTHS = 3       # at least this many distinct calendar months in-window
MIN_PER_MONTH = 1.0         # averaged over the months their activity spanned

DISMISS_KV_KEY = "radar_dismissed"
BACKFILL_DONE_KEY = "radar_backfill_done:"      # + lowercased account address
BACKFILL_PROGRESS_KEY = "radar_backfill_progress:"

# Addresses that are technically a person but never a relationship.
_BULK_LOCALPARTS = {
    "noreply", "no-reply", "donotreply", "do-not-reply", "notifications",
    "notification", "newsletter", "news", "bounce", "bounces", "mailer",
    "mailer-daemon", "postmaster", "automated", "auto-confirm", "alerts",
}
_LIST_LOCAL_RE = re.compile(r"^(.*[-.])?(bounce|noreply|no-reply|notifications?)([-.].*)?$", re.I)


def _now(now=None) -> datetime:
    if now is None:
        return datetime.now(timezone.utc)
    if isinstance(now, str):
        return _parse_dt(now) or datetime.now(timezone.utc)
    return now if now.tzinfo else now.replace(tzinfo=timezone.utc)


def is_bulk_address(address: str) -> bool:
    """no-reply / bulk-looking. NOREPLY_RE is the shared rule; the local-part
    checks catch the rest of the usual suspects."""
    a = (address or "").strip().lower()
    if not a or "@" not in a or " " in a:
        return True
    if NOREPLY_RE.search(a):
        return True
    local = a.split("@", 1)[0]
    return local in _BULK_LOCALPARTS or bool(_LIST_LOCAL_RE.match(local))


def _user_addresses(primary: str, db_file=None) -> set[str]:
    """Every address that counts as 'me' - the account being scanned plus every
    other configured inbox, so mail between your own accounts is never a
    relationship and a reply sent from inbox B still reads as 'out'.

    Defers to poller.own_addresses, which is the canonical multi-inbox identity
    set (the poller uses it to decide what is own mail); the direct accounts
    query is only a fallback."""
    addrs = {(primary or "").strip().lower()}
    try:
        try:
            from . import config, poller
        except ImportError:                       # standalone, pre-integration
            from mailpilot import config, poller
        addrs |= {(a or "").strip().lower()
                  for a in poller.own_addresses(config.load(), db_file)}
    except Exception:
        try:
            with db.conn(db_file) as c:
                for r in c.execute("SELECT address FROM accounts").fetchall():
                    addrs.add((r["address"] or "").strip().lower())
        except Exception:
            pass
    return {a for a in addrs if a}


# ------------------------------------------------------------------- recording

def _insert_rows(rows, db_file=None) -> int:
    """Dedupe on (address, direction, date) so a re-run adds nothing."""
    if not rows:
        return 0
    added = 0
    with db.conn(db_file) as c:
        for address, direction, date_iso in rows:
            cur = c.execute(
                "INSERT INTO correspondence_log (address, direction, date)"
                " SELECT ?,?,? WHERE NOT EXISTS ("
                "  SELECT 1 FROM correspondence_log WHERE address=? AND direction=? AND date=?)",
                (address, direction, date_iso, address, direction, date_iso),
            )
            added += cur.rowcount or 0
    return added


def record(address_of_counterpart: str, direction: str, date_iso: str = "", db_file=None) -> bool:
    """The cheap forward hook: call this from the live poller (direction 'in',
    after storing an email) and from the sender (direction 'out', after a
    successful send). Best-effort - it must never break the path it is bolted
    onto - but a failure is recorded, not swallowed silently.
    """
    try:
        address = (address_of_counterpart or "").strip().lower()
        if direction not in ("in", "out") or is_bulk_address(address):
            return False
        return _insert_rows([(address, direction, date_iso or db.now_iso())], db_file) > 0
    except Exception as e:
        db.record_error("radar", f"correspondence_log write failed: {e}",
                        traceback.format_exc(), db_file)
        return False


# -------------------------------------------------------------------- backfill

def kv_progress_cb(address: str, db_file=None):
    """Progress callback that parks JSON in kv, for the threaded endpoint.
    Read it back with kv_get(BACKFILL_PROGRESS_KEY + address.lower())."""
    key = BACKFILL_PROGRESS_KEY + (address or "").strip().lower()

    def cb(done: int, total: int, phase: str):
        db.kv_set(key, json.dumps({
            "done": done, "total": total, "phase": phase, "at": db.now_iso(),
        }), db_file)
    return cb


def backfill(address: str, app_password: str, months: int = 12, cap: int = 5000,
             progress_cb=None, force: bool = False, db_file=None) -> dict:
    """Header-only scan of the last `months` of mail into correspondence_log.

    Reads All Mail when it exists (one pass covers both directions); otherwise
    INBOX plus the Sent folder. Only FROM/TO/DATE are fetched, with BODY.PEEK,
    from a mailbox opened readonly - nothing on the server changes.

    Idempotent twice over: the kv flag 'radar_backfill_done:<address>' short-
    circuits a second run (pass force=True to rescan), and every insert dedupes
    on (address, direction, date), so even a forced rescan adds only new mail.

    `cap` bounds the number of messages examined per mailbox - this runs on a
    click and a long-lived Gmail account has six figures of mail.

    progress_cb(done, total, phase) is called as it goes; exceptions from it are
    ignored so a bad callback cannot kill a scan.
    """
    account = (address or "").strip().lower()
    done_key = BACKFILL_DONE_KEY + account
    if not force and db.kv_get(done_key, db_file):
        return {"skipped": True, "reason": "already_done", "scanned": 0, "added": 0}

    def progress(done, total, phase):
        if progress_cb is None:
            return
        try:
            progress_cb(done, total, phase)
        except Exception:
            pass

    since = (_now() - timedelta(days=30 * max(1, int(months)))).strftime("%d-%b-%Y")
    mine = _user_addresses(account, db_file)
    cap = max(1, int(cap))
    scanned = added = 0
    try:
        with imap_session(account, app_password) as conn:
            all_mail = find_all_mail_folder(conn)
            if all_mail and all_mail.upper() != "INBOX":
                mailboxes = [all_mail]
            else:
                mailboxes = [m for m in ("INBOX", find_sent_folder(conn)) if m]
            for mailbox in mailboxes:
                if not select_readonly(conn, mailbox):
                    continue
                typ, data = conn.search(None, "SINCE", since)
                if typ != "OK":
                    continue
                seqs = [int(n) for n in (data[0] or b"").split()]
                seqs = seqs[-cap:]                       # newest `cap` messages
                total = len(seqs)
                progress(0, total, mailbox)
                for i in range(0, total, 200):
                    chunk = seqs[i:i + 200]
                    rows = []
                    for _seq, msg in fetch_headers(conn, chunk, "FROM TO DATE"):
                        scanned += 1
                        parsed = _row_from_headers(msg, mine)
                        if parsed:
                            rows.append(parsed)
                    added += _insert_rows(rows, db_file)
                    progress(min(i + 200, total), total, mailbox)
    except Exception as e:
        db.record_error("radar", f"Backfill failed for {account}: {e}",
                        traceback.format_exc(), db_file)
        raise

    db.kv_set(done_key, json.dumps({
        "finished_at": db.now_iso(), "months": int(months),
        "scanned": scanned, "added": added,
    }), db_file)
    progress(scanned, scanned, "done")
    return {"skipped": False, "scanned": scanned, "added": added, "since": since}


def _row_from_headers(msg, mine: set[str]):
    """One header block -> (counterpart_address, direction, date_iso) or None."""
    _n, from_addr = email.utils.parseaddr(msg.get("From", "") or "")
    from_addr = (from_addr or "").strip().lower()
    if not from_addr:
        return None
    if from_addr in mine:
        direction = "out"
        counterpart = ""
        for _name, addr in email.utils.getaddresses(msg.get_all("To") or []):
            addr = (addr or "").strip().lower()
            if addr and addr not in mine:
                counterpart = addr
                break
        if not counterpart:
            return None                       # note-to-self, or To: only my own inboxes
    else:
        direction = "in"
        counterpart = from_addr
    if is_bulk_address(counterpart):
        return None
    return counterpart, direction, _date_iso(msg.get("Date", ""))


def _date_iso(raw: str) -> str:
    """Date: header -> ISO-8601 TEXT with offset (house rule: never naive)."""
    try:
        dt = email.utils.parsedate_to_datetime(raw)
    except (TypeError, ValueError):
        return db.now_iso()
    if dt is None:
        return db.now_iso()
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc).astimezone()
    return dt.isoformat(timespec="seconds")


# ------------------------------------------------------------------- dismissal

def _dismissed_map(db_file=None) -> dict:
    try:
        raw = db.kv_get(DISMISS_KV_KEY, db_file)
        data = json.loads(raw) if raw else {}
        return data if isinstance(data, dict) else {}
    except (json.JSONDecodeError, TypeError):
        return {}


def dismiss(address: str, days: int = 90, db_file=None) -> str:
    """Hide an address from the radar until `days` from now. Snooze, not delete:
    a relationship that lapses again after the snooze is a fresh finding."""
    address = (address or "").strip().lower()
    if not address:
        return ""
    until = (_now() + timedelta(days=max(1, int(days)))).isoformat(timespec="seconds")
    data = _dismissed_map(db_file)
    data[address] = until
    now = _now()
    data = {a: u for a, u in data.items() if (_parse_dt(u) or now) > now}
    data[address] = until                     # keep it even if the prune raced
    db.kv_set(DISMISS_KV_KEY, json.dumps(data), db_file)
    return until


def undismiss(address: str, db_file=None) -> None:
    data = _dismissed_map(db_file)
    if data.pop((address or "").strip().lower(), None) is not None:
        db.kv_set(DISMISS_KV_KEY, json.dumps(data), db_file)


def _active_dismissals(now: datetime, db_file=None) -> set[str]:
    return {a for a, until in _dismissed_map(db_file).items()
            if (_parse_dt(until) or now) > now}


# ----------------------------------------------------------------------- drift

def _cadence_word(per_month: float) -> str:
    if per_month >= 12:
        return "most days"
    if per_month >= 4:
        return "weekly"
    if per_month >= 2:
        return "every couple of weeks"
    return "monthly"


def _name_hint(address: str, known: dict) -> str:
    """A display name: the People entry if there is one, else the local part
    cleaned up. Never guessed beyond that - a wrong name in a nudge is worse
    than no name."""
    a = (address or "").lower()
    if known.get(a):
        return known[a]
    local = a.split("@", 1)[0]
    parts = [p for p in re.split(r"[._\-+]+", local) if p and not p.isdigit()]
    return " ".join(p.capitalize() for p in parts) if parts else a


def compute_drift(now=None, db_file=None) -> list[dict]:
    """Addresses whose cadence has dropped off. Pure Python, no LLM.

    An address qualifies when, in the baseline window running from 12 months ago
    to 2 months ago, it had:
      * mail in at least MIN_ACTIVE_MONTHS (3) distinct calendar months, AND
      * an average of at least MIN_PER_MONTH (1) exchange per month across the
        span its activity actually covered (first in-window exchange -> last,
        measured in days so a cadence isn't diluted by partial end months),
    AND has had nothing in either direction for at least QUIET_DAYS (60) days.

    Sorted by historical volume (in-window exchange count) descending.
    """
    now = _now(now)
    win_start = now - timedelta(days=BASELINE_START_DAYS)
    win_end = now - timedelta(days=BASELINE_END_DAYS)
    quiet_before = now - timedelta(days=QUIET_DAYS)

    with db.conn(db_file) as c:
        rows = c.execute(
            "SELECT address, direction, date FROM correspondence_log"
        ).fetchall()
        known = {}
        try:
            for r in c.execute("SELECT address, name FROM contacts").fetchall():
                if r["name"]:
                    known[(r["address"] or "").lower()] = r["name"]
        except Exception:
            pass

    per_address: dict[str, dict] = {}
    for r in rows:
        address = (r["address"] or "").strip().lower()
        if not address or is_bulk_address(address):
            continue            # defence in depth: also filtered at insert time
        dt = _parse_dt(r["date"])
        if dt is None:
            continue
        slot = per_address.setdefault(
            address, {"months": {}, "in_window": 0, "last": None, "first_in": None, "last_in": None})
        if slot["last"] is None or dt > slot["last"]:
            slot["last"] = dt
        if win_start <= dt <= win_end:
            slot["in_window"] += 1
            slot["months"][dt.strftime("%Y-%m")] = True
            if slot["first_in"] is None or dt < slot["first_in"]:
                slot["first_in"] = dt
            if slot["last_in"] is None or dt > slot["last_in"]:
                slot["last_in"] = dt

    dismissed = _active_dismissals(now, db_file)
    out = []
    for address, slot in per_address.items():
        if address in dismissed:
            continue
        if len(slot["months"]) < MIN_ACTIVE_MONTHS:
            continue
        span_months = _span_months(slot["first_in"], slot["last_in"])
        per_month = slot["in_window"] / span_months
        if per_month < MIN_PER_MONTH:
            continue
        last = slot["last"]
        if last is None or last > quiet_before:
            continue                              # still talking - not a finding
        days_since = max(0, int((now - last).total_seconds() // 86400))
        shown = max(1, round(span_months))
        out.append({
            "address": address,
            "name_hint": _name_hint(address, known),
            "baseline": f"{_cadence_word(per_month)} for {shown} month{'s' if shown != 1 else ''}",
            "last_contact_iso": last.isoformat(timespec="seconds"),
            "weeks_since": days_since // 7,
            "exchanges": slot["in_window"],
        })
    out.sort(key=lambda d: (-d["exchanges"], d["address"]))
    return out


AVG_MONTH_DAYS = 30.44


def _span_months(first: datetime, last: datetime) -> float:
    """Length of the active span in months, measured in days. Calendar-month
    counting inflates the span (an exchange on the 28th and one on the 2nd is
    'two months'), which drags a genuinely weekly cadence below the threshold."""
    if first is None or last is None:
        return 1.0
    days = max(0.0, (last - first).total_seconds() / 86400.0)
    return max(1.0, days / AVG_MONTH_DAYS)


# ------------------------------------------------------- reconnection drafting

def build_reconnect_prompt(address: str, name_hint: str, weeks_since: int,
                           baseline: str = "") -> str:
    """User prompt for the reconnection nudge. Deliberately thin on facts: the
    model knows nothing about why the thread stopped and must not invent a
    reason, a shared memory, or a commitment."""
    who = name_hint or address
    return (
        "Draft a SHORT, warm reconnection email (3-5 sentences) to someone the "
        f"user corresponded with regularly ({baseline or 'for months'}) and has "
        f"not exchanged mail with for about {max(1, int(weeks_since))} weeks.\n\n"
        f"Recipient: {who} <{address}>\n\n"
        "Rules for this one:\n"
        "- Do not invent a reason for the silence, a shared memory, a project, or "
        "any news. You know nothing beyond the gap itself.\n"
        "- Acknowledge the gap lightly, ask an open question, make it easy to "
        "ignore without guilt.\n"
        "- No subject line. Plain text only.\n"
        "- If something specific is needed to make it land, leave a "
        "[FILL IN: ...] placeholder - the send guard blocks those, so the user "
        "must fill it in before it can go."
    )


# ----------------------------------------------------------------- offline check

if __name__ == "__main__":
    import tempfile
    from pathlib import Path

    tmp = Path(tempfile.mkdtemp(prefix="mailpilot-radar-")) / "t.db"
    db.bootstrap(tmp)
    with db.conn(tmp) as c:
        c.executescript(CORRESPONDENCE_LOG_DDL)

    NOW = datetime(2026, 9, 16, 12, 0, 0, tzinfo=timezone.utc)
    checks = []

    def check(name, cond):
        checks.append((name, bool(cond)))
        print(("  ok   " if cond else "  FAIL ") + name)

    def seed(address, first_days_ago, last_days_ago, every_days):
        """Synthetic history: one exchange every `every_days` across a span."""
        rows, d = [], first_days_ago
        i = 0
        while d >= last_days_ago:
            when = (NOW - timedelta(days=d)).isoformat(timespec="seconds")
            rows.append((address, "in" if i % 2 else "out", when))
            d -= every_days
            i += 1
        _insert_rows(rows, tmp)
        return len(rows)

    print("radar.py offline checks (no network, no LLM)")

    # qualifies: weekly for ~9 months, silent for ~3 months
    n_q = seed("qualifies@example.com", 360, 92, 7)
    # too recent: same cadence, but they wrote last week
    seed("recent@example.com", 360, 6, 7)
    # too sparse: two exchanges, two months, long silent
    _insert_rows([("sparse@example.com", "in", (NOW - timedelta(days=300)).isoformat(timespec="seconds")),
                  ("sparse@example.com", "out", (NOW - timedelta(days=270)).isoformat(timespec="seconds"))], tmp)
    # active enough months but under one/month across the span they cover
    _insert_rows([("thin@example.com", "in", (NOW - timedelta(days=d)).isoformat(timespec="seconds"))
                  for d in (350, 260, 170)], tmp)
    # dismissed: identical profile to the qualifier
    seed("dismissed@example.com", 360, 92, 7)
    # bulk: would qualify on volume, must never surface
    seed("no-reply@newsletter.example.com", 360, 92, 7)
    seed("notifications@example.com", 360, 92, 7)

    dismiss("dismissed@example.com", days=90, db_file=tmp)

    drift = compute_drift(now=NOW, db_file=tmp)
    found = {d["address"]: d for d in drift}

    check("a lapsed weekly correspondent is found", "qualifies@example.com" in found)
    check("someone who wrote last week is NOT found", "recent@example.com" not in found)
    check("a two-exchange contact is NOT found", "sparse@example.com" not in found)
    check("under one exchange/month is NOT found", "thin@example.com" not in found)
    check("a dismissed address is NOT found", "dismissed@example.com" not in found)
    check("no-reply/bulk addresses are NOT found",
          not any("no-reply" in a or a.startswith("notifications@") for a in found))

    q = found.get("qualifies@example.com", {})
    check("baseline reads like a cadence", q.get("baseline", "").startswith("weekly for "))
    check("weeks_since is about 13", 12 <= q.get("weeks_since", 0) <= 14)
    check("last_contact_iso carries an offset",
          len(q.get("last_contact_iso", "")) > 19 and q["last_contact_iso"][-6] in "+-Z")
    check("name_hint is derived from the local part", q.get("name_hint") == "Qualifies")
    check("exchange count matches the seeded history", q.get("exchanges") == n_q)

    with db.conn(tmp) as c:
        c.execute("INSERT INTO contacts (address, name, rule, created_at) VALUES (?,?,?,?)",
                  ("qualifies@example.com", "Dana Reyes", "normal", db.now_iso()))
    check("a People entry supplies the name",
          compute_drift(now=NOW, db_file=tmp)[0]["name_hint"] == "Dana Reyes")

    # sorted by volume desc
    seed("louder@example.com", 360, 92, 3)
    ordered = compute_drift(now=NOW, db_file=tmp)
    check("sorted by historical volume, loudest first",
          ordered[0]["address"] == "louder@example.com")

    # dismissal expiry
    dismiss("qualifies@example.com", days=90, db_file=tmp)
    check("dismissal hides it immediately",
          "qualifies@example.com" not in {d["address"] for d in compute_drift(now=NOW, db_file=tmp)})
    check("dismissal expires - it is a snooze, not a delete",
          "qualifies@example.com" in {d["address"] for d in
                                      compute_drift(now=NOW + timedelta(days=100), db_file=tmp)})
    undismiss("qualifies@example.com", db_file=tmp)
    check("undismiss restores it",
          "qualifies@example.com" in {d["address"] for d in compute_drift(now=NOW, db_file=tmp)})

    # record() hook
    before = len(compute_drift(now=NOW, db_file=tmp))
    check("record() writes an exchange",
          record("qualifies@example.com", "in", (NOW - timedelta(days=1)).isoformat(), db_file=tmp))
    check("record() is idempotent on (address, direction, date)",
          not record("qualifies@example.com", "in", (NOW - timedelta(days=1)).isoformat(), db_file=tmp))
    check("fresh contact drops off the radar after record()",
          "qualifies@example.com" not in {d["address"] for d in compute_drift(now=NOW, db_file=tmp)}
          and before > 0)
    check("record() refuses a bulk address", not record("no-reply@x.example.com", "in", db_file=tmp))
    check("record() refuses a bad direction", not record("someone@example.com", "sideways", db_file=tmp))

    # header -> row classification, no server involved
    mine = {"pilot@example.com", "second@example.com"}

    def hdrs(frm, to):
        return email.message_from_string(f"From: {frm}\nTo: {to}\nDate: Tue, 12 Aug 2025 09:13:00 +0200\n\n")

    check("inbound mail is 'in' keyed on the sender",
          _row_from_headers(hdrs("Alice <alice@example.com>", "pilot@example.com"), mine)[:2]
          == ("alice@example.com", "in"))
    check("sent mail is 'out' keyed on the recipient",
          _row_from_headers(hdrs("pilot@example.com", "Bob <bob@example.com>"), mine)[:2]
          == ("bob@example.com", "out"))
    check("mail between my own inboxes is ignored",
          _row_from_headers(hdrs("pilot@example.com", "second@example.com"), mine) is None)
    check("a no-reply sender is ignored",
          _row_from_headers(hdrs("no-reply@shop.example.com", "pilot@example.com"), mine) is None)
    check("header date keeps its offset",
          _row_from_headers(hdrs("alice@example.com", "pilot@example.com"), mine)[2].endswith("+02:00"))

    # backfill idempotency guard, without touching IMAP
    db.kv_set(BACKFILL_DONE_KEY + "pilot@example.com", json.dumps({"finished_at": db.now_iso()}), tmp)
    r = backfill("pilot@example.com", "never-used", db_file=tmp)
    check("backfill short-circuits on the kv done flag",
          r["skipped"] and r["reason"] == "already_done")

    p = build_reconnect_prompt("dana@example.com", "Dana", 13, "weekly for 9 months")
    check("reconnect prompt forbids invented context", "Do not invent" in p)

    bad = sum(1 for _, ok in checks if not ok)
    print(f"\n{len(checks) - bad}/{len(checks)} checks passed")
    raise SystemExit(1 if bad else 0)
