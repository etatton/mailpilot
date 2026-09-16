"""The ONE send choke-point. No other module imports smtplib for outbound mail.

send_reply(draft_id) runs ordered guards; the first failure sets the draft to
'blocked' with a named reason and returns. Only a draft the user explicitly
approved can ever reach the wire, and with live_send off nothing does.
"""
import email.utils
import re
import smtplib
import traceback
from email.message import EmailMessage

from . import config, db

HTML_DOC_RE = re.compile(r"<!doctype html|<html[\s>]", re.I)


def _block(c, draft_id: int, reason: str) -> dict:
    c.execute(
        "UPDATE drafts SET status='blocked', block_reason=?, updated_at=? WHERE id=?",
        (reason, db.now_iso(), draft_id),
    )
    return {"ok": False, "status": "blocked", "reason": reason}


def send_reply(draft_id: int, cfg: dict = None, db_file=None, smtp_factory=None) -> dict:
    """smtp_factory exists so the self-test can prove no socket is ever opened
    on a guarded path - production always uses the real one."""
    cfg = cfg or config.load()
    with db.conn(db_file) as c:
        draft = c.execute("SELECT * FROM drafts WHERE id=?", (draft_id,)).fetchone()
        if draft is None:
            return {"ok": False, "status": "missing", "reason": "no_such_draft"}

        # Guard 1 - explicit approval, nothing else, opens the gate
        if draft["status"] != "approved":
            return _block(c, draft_id, f"not_approved (status={draft['status']})")

        em = c.execute("SELECT * FROM emails WHERE id=?", (draft["email_id"],)).fetchone()
        if em is None:
            return _block(c, draft_id, "original_email_missing")

        # Guard 2 - the inbox this email arrived in must still exist. Its address
        # is the From line and its stored app password is the SMTP login, so a
        # deleted account has no identity to reply AS. Blocks in test mode too.
        account_id = em["account_id"] if "account_id" in em.keys() else 1
        account = c.execute(
            "SELECT * FROM accounts WHERE id=?", (account_id,)
        ).fetchone()
        if account is None:
            return _block(c, draft_id, "account_missing")
        from_address = account["address"]

        # Guard 3 - recipient must parse as one valid address
        _, recipient = email.utils.parseaddr(em["from_address"])
        if not recipient or "@" not in recipient or " " in recipient:
            return _block(c, draft_id, "invalid_recipient")

        # Guard 4 - never send to the ignore list
        from .poller import _addr_matches
        if _addr_matches(recipient, cfg.get("ignore_senders") or []):
            return _block(c, draft_id, "recipient_on_ignore_list")

        # Guard 5 - no double sends of the same KIND for one message (a follow-up
        # nudge after a sent reply is legitimate; a second reply or second nudge is not)
        kind = draft["kind"] if "kind" in draft.keys() else "reply"
        dup = c.execute(
            "SELECT d.id FROM drafts d JOIN emails e ON e.id=d.email_id"
            " WHERE e.message_id=? AND d.status='sent' AND d.kind=? AND d.id != ?",
            (em["message_id"], kind, draft_id),
        ).fetchone()
        if dup:
            return _block(c, draft_id, "already_sent_for_this_message")

        # Guard 6 - body sanity
        body = (draft["body"] or "").strip()
        if not body:
            return _block(c, draft_id, "empty_body")
        if "[FILL IN" in body:
            return _block(c, draft_id, "unresolved_fill_in_placeholder")
        if HTML_DOC_RE.search(body):
            return _block(c, draft_id, "html_document_in_body")

        # Guard 7 - the live gate: everything ran, nothing touches SMTP
        if not cfg.get("live_send"):
            c.execute(
                "UPDATE drafts SET status='simulated', updated_at=?, sent_at=? WHERE id=?",
                (db.now_iso(), db.now_iso(), draft_id),
            )
            return {"ok": True, "status": "simulated", "recipient": recipient,
                    "account": from_address}

        subject = em["subject"] or ""
        if kind == "reconnect":
            # A reconnection is a fresh email, not a reply - no "Re:" for a
            # conversation the recipient never started.
            subject = subject or "Hello"
        elif not re.match(r"^\s*re\s*:", subject, re.I):
            subject = "Re: " + subject

    # The reply goes out AS the inbox it arrived in - address and credential both.
    from .poller import account_password
    password = account_password(account, db_file)
    if not password:
        with db.conn(db_file) as c:
            return _block(c, draft_id, f"no_app_password for {from_address}")

    msg = EmailMessage()
    msg["From"] = email.utils.formataddr((cfg.get("signature_name") or "", from_address))
    msg["To"] = recipient
    msg["Subject"] = subject
    if em["message_id"] and kind != "reconnect":
        # A reconnect's seed message-id is synthetic - never put it in a real
        # threading header.
        msg["In-Reply-To"] = em["message_id"]
        refs = (em["thread_references"] + " " + em["message_id"]).strip()
        msg["References"] = refs
    msg.set_content(body)

    try:
        factory = smtp_factory or (lambda: smtplib.SMTP_SSL("smtp.gmail.com", 465, timeout=30))
        smtp = factory()
        try:
            smtp.login(from_address, password)
            smtp.send_message(msg)
        finally:
            try:
                smtp.quit()
            except Exception:
                pass
    except Exception as e:
        db.record_error("sender", f"Send failed to {recipient}: {e}", traceback.format_exc(), db_file)
        with db.conn(db_file) as c:
            return _block(c, draft_id, f"smtp_error: {str(e)[:200]}")

    with db.conn(db_file) as c:
        c.execute(
            "UPDATE drafts SET status='sent', updated_at=?, sent_at=? WHERE id=?",
            (db.now_iso(), db.now_iso(), draft_id),
        )
    if cfg.get("feature_radar"):
        # Real sends only - a simulated send reached nobody and must not reset
        # anyone's drift clock.
        from . import radar
        radar.record(recipient, "out", db_file=db_file)
    return {"ok": True, "status": "sent", "recipient": recipient,
            "account": from_address}


def send_self_notification(subject: str, body: str, cfg: dict = None) -> bool:
    """The ONLY other SMTP path, and it is hard-wired to the user's own address -
    it cannot be pointed anywhere else. Always the PRIMARY inbox, whatever other
    inboxes exist, so notices land in one predictable place.
    Best-effort: failures are recorded (they surface in the UI) but never raise."""
    cfg = cfg or config.load()
    address = (cfg.get("gmail_address") or "").strip()
    password = ""
    if address:
        password = (config.get_secret(config.gmail_secret_name(address))
                    or config.get_secret("gmail_app_password"))
    if not (address and password):
        return False
    msg = EmailMessage()
    msg["From"] = email.utils.formataddr(("MailPilot", address))
    msg["To"] = address          # self, always - never a parameter
    msg["Subject"] = subject
    msg.set_content(body)
    try:
        smtp = smtplib.SMTP_SSL("smtp.gmail.com", 465, timeout=30)
        try:
            smtp.login(address, password)
            smtp.send_message(msg)
        finally:
            try:
                smtp.quit()
            except Exception:
                pass
        return True
    except Exception as e:
        db.record_error("notify", f"Notification email failed: {e}", traceback.format_exc())
        return False
