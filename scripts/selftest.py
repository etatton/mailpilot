"""Guard self-test. Proves the send choke-point can say NO.

Run: python scripts/selftest.py
Every check uses a temp database and a booby-trapped SMTP factory that raises
if anything ever tries to open a connection - so a passing run is positive
proof that no guarded path reaches the network. Delete or weaken a guard in
sender.py and at least one assertion here fails.
"""
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from mailpilot import db, sender  # noqa: E402

CFG = {
    "gmail_address": "pilot@example.com",
    "signature_name": "Pilot",
    "ignore_senders": ["blocked.example.com"],
    "live_send": False,
}


def boom():
    raise AssertionError("SMTP was touched on a guarded path - a guard is broken!")


def seed_account(dbf):
    """Account 1 is the inbox every seeded email arrives in. Forced to the test
    address so a real config on the machine can't leak into the fixture."""
    with db.conn(dbf) as c:
        c.execute(
            "INSERT INTO accounts (id, address, label, created_at) VALUES (1,?,?,?)"
            " ON CONFLICT(id) DO UPDATE SET address=excluded.address",
            (CFG["gmail_address"], "Primary", db.now_iso()),
        )


def seed(dbf, from_address="alice@example.com", body="Thanks Alice, sounds good.\n\nPilot",
         draft_status="queued", message_id=None, kind="reply", email_id=None,
         account_id=1):
    with db.conn(dbf) as c:
        if email_id is None:
            cur = c.execute(
                "INSERT INTO emails (message_id, from_address, from_name, subject, body_text,"
                " received_at, processed_at, account_id) VALUES (?,?,?,?,?,?,?,?)",
                (message_id or f"<m{c.execute('SELECT COUNT(*) FROM emails').fetchone()[0]}@x>",
                 from_address, "Alice", "Hello", "Hi there", db.now_iso(), db.now_iso(),
                 account_id),
            )
            email_id = cur.lastrowid
        cur = c.execute(
            "INSERT INTO drafts (email_id, body, status, kind, created_at, updated_at)"
            " VALUES (?,?,?,?,?,?)",
            (email_id, body, draft_status, kind, db.now_iso(), db.now_iso()),
        )
        return cur.lastrowid, email_id


def main():
    tmp = Path(tempfile.mkdtemp()) / "test.db"
    db.bootstrap(tmp)
    seed_account(tmp)
    failures = []

    def check(name, result, want_status, want_reason_part=""):
        ok = result["status"] == want_status and want_reason_part in result.get("reason", "")
        print(f"  {'PASS' if ok else 'FAIL'}  {name}: {result}")
        if not ok:
            failures.append(name)

    # (a) an unapproved draft can never send
    d1, _ = seed(tmp, draft_status="queued")
    check("unapproved draft blocks",
          sender.send_reply(d1, CFG, tmp, smtp_factory=boom), "blocked", "not_approved")

    # (b) approved + test mode -> simulated, zero network
    d2, _ = seed(tmp, draft_status="approved")
    check("test mode simulates without SMTP",
          sender.send_reply(d2, CFG, tmp, smtp_factory=boom), "simulated")

    # (c) live mode + unresolved placeholder blocks (still zero network)
    live = dict(CFG, live_send=True)
    d3, _ = seed(tmp, draft_status="approved", body="Hi [FILL IN: price] thanks")
    check("[FILL IN placeholder blocks",
          sender.send_reply(d3, live, tmp, smtp_factory=boom), "blocked", "fill_in")

    # (d) live mode + recipient on the ignore list blocks
    d4, _ = seed(tmp, draft_status="approved", from_address="bob@blocked.example.com")
    check("ignore-list recipient blocks",
          sender.send_reply(d4, live, tmp, smtp_factory=boom), "blocked", "ignore_list")

    # (e) a second send for the same original message blocks
    d5, e5 = seed(tmp, draft_status="approved", message_id="<dup@x>")
    with db.conn(tmp) as c:  # pretend an earlier draft already went out for this message
        c.execute("INSERT INTO drafts (email_id, body, status, created_at, updated_at)"
                  " VALUES (?,?,?,?,?)", (e5, "sent earlier", "sent", db.now_iso(), db.now_iso()))
    check("duplicate send blocks",
          sender.send_reply(d5, live, tmp, smtp_factory=boom), "blocked", "already_sent")

    # (f) an HTML error page in the body blocks
    d6, _ = seed(tmp, draft_status="approved", body="<!doctype html><html>502 Bad Gateway</html>")
    check("HTML document body blocks",
          sender.send_reply(d6, live, tmp, smtp_factory=boom), "blocked", "html_document")

    # (g) a follow-up nudge after a SENT reply passes guard 4 (kind-scoped)...
    d7, e7 = seed(tmp, draft_status="sent", message_id="<fu@x>", kind="reply")
    f1, _ = seed(tmp, draft_status="approved", kind="followup", email_id=e7,
                 body="Just floating this back up. Pilot")
    check("follow-up after sent reply is allowed",
          sender.send_reply(f1, CFG, tmp, smtp_factory=boom), "simulated")

    # ...but a SECOND follow-up for the same message blocks
    with db.conn(tmp) as c:
        c.execute("UPDATE drafts SET status='sent' WHERE id=?", (f1,))
    f2, _ = seed(tmp, draft_status="approved", kind="followup", email_id=e7,
                 body="Another nudge. Pilot")
    check("second follow-up blocks",
          sender.send_reply(f2, live, tmp, smtp_factory=boom), "blocked", "already_sent")

    # (h) a draft whose inbox no longer exists has no identity to send AS - it must
    # block before SMTP rather than quietly falling back to some other address
    d8, _ = seed(tmp, draft_status="approved", account_id=999)
    check("missing account blocks",
          sender.send_reply(d8, live, tmp, smtp_factory=boom), "blocked", "account_missing")

    # (i) a Radar reconnect draft goes through the SAME guards - test mode
    # simulates it, no special-casing around the choke-point
    d9, _ = seed(tmp, draft_status="approved", kind="reconnect",
                 message_id="<radar-seed-abc@mailpilot>",
                 body="Hi Alice - been a while! How have you been?\n\nPilot")
    check("reconnect draft simulates through the same guards",
          sender.send_reply(d9, CFG, tmp, smtp_factory=boom), "simulated")

    print()
    if failures:
        print(f"SELF-TEST FAILED: {failures}")
        return 1
    print("SELF-TEST PASSED: all guards hold; no guarded path touched SMTP.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
