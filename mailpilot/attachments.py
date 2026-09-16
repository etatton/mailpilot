"""Honest attachment handling.

MailPilot drafts replies from body text alone today - an attachment is
invisible to the model even though the human sees "invoice attached" and
expects the reply to react to it. The fix has a sharp edge: a model asked
to write about an attachment it never saw will happily invent plausible
content for it. So this module does two things, deliberately kept together:

1. extract_attachments() pulls out every attachment, and for the small,
   genuinely-text ones (plain/csv/markdown, capped), a real excerpt of
   their content - honestly capped, never claiming more than was read.
2. attachment_context() turns that into a prompt paragraph that is just as
   explicit about what WASN'T read as what was - "You have NOT read
   invoice.pdf" is the load-bearing sentence here, not a nicety. Without
   it the model fills the gap with a guess and the reply states it as fact.
"""
import email.header

TEXT_CONTENT_TYPES = {"text/plain", "text/csv", "text/markdown"}
MAX_TEXT_SOURCE_BYTES = 100_000   # only excerpt a text part this size or smaller
MAX_TEXT_CHARS = 4000             # ...and cap the excerpt itself to this many chars


def _decode_filename(raw) -> str:
    """part.get_filename() already resolves RFC2231 (charset'lang'value)
    continuations but not RFC2047 encoded-words (=?utf-8?B?...?=), which
    older mail clients still send. Decode those the same way store_email()
    decodes Subject in poller.py."""
    if not raw:
        return raw
    try:
        pieces = email.header.decode_header(raw)
        return "".join(
            p.decode(enc or "utf-8", errors="replace") if isinstance(p, bytes) else p
            for p, enc in pieces
        )
    except Exception:
        return raw


def extract_attachments(msg) -> list[dict]:
    """Every part with a filename or an attachment Content-Disposition -
    inline images included, since those are still content a client attached,
    not part of the readable body. Returns a list of:
        {"name": str, "content_type": str, "size": int, "text": str|None}
    "text" is set (capped MAX_TEXT_CHARS) only for text/plain, text/csv,
    text/markdown parts whose RAW size is <= MAX_TEXT_SOURCE_BYTES; it is
    None for everything else (a PDF, an oversized text file, an image, a
    docx, ...) - None is the "unreadable" signal attachment_context() acts on.
    """
    out: list[dict] = []
    if not msg.is_multipart():
        return out  # a bare single-part message has no attachments, only a body

    for part in msg.walk():
        if part.is_multipart():
            continue  # container, not a leaf part

        filename = _decode_filename(part.get_filename())
        disposition = (part.get_content_disposition() or "").lower()
        if not filename and disposition != "attachment":
            continue  # this is the message body (plain/html), not an attachment

        content_type = part.get_content_type()
        try:
            payload = part.get_payload(decode=True)
        except Exception:
            payload = None
        size = len(payload) if payload is not None else 0
        name = filename or f"attachment-{len(out) + 1}"

        item = {"name": name, "content_type": content_type, "size": size, "text": None}
        if (content_type in TEXT_CONTENT_TYPES and payload is not None
                and size <= MAX_TEXT_SOURCE_BYTES):
            charset = part.get_content_charset() or "utf-8"
            try:
                text = payload.decode(charset, errors="replace")
            except (LookupError, ValueError):
                text = payload.decode("utf-8", errors="replace")
            item["text"] = text.strip()[:MAX_TEXT_CHARS]
        out.append(item)

    return out


def attachment_context(attachments: list[dict]) -> str:
    """A prompt paragraph: readable attachments inlined, unreadable ones
    named with an explicit "I have not read this" instruction. Returns ""
    for no attachments, so callers can always do
    `prompt += attachment_context(...)` with no empty-section check."""
    if not attachments:
        return ""
    lines = ["This email has attachments:"]
    for a in attachments:
        if a.get("text"):
            lines.append(f"- Attachment {a['name']} contains: {a['text']}")
        else:
            lines.append(
                f"- You have NOT read {a['name']} ({a['content_type']}). "
                "Do not claim or imply you read it; if the reply depends on "
                "it, say you'll review it."
            )
    return "\n".join(lines)


# ------------------------------------------------------------------ offline

if __name__ == "__main__":
    import sys as _sys
    from email.mime.application import MIMEApplication
    from email.mime.multipart import MIMEMultipart
    from email.mime.text import MIMEText

    _fail = []

    def _check(label, cond):
        print(("PASS " if cond else "FAIL ") + label)
        if not cond:
            _fail.append(label)

    # ---- synthetic multipart email: body + small csv + "pdf" + oversized text
    msg = MIMEMultipart()
    msg["From"] = "buyer@example.com"
    msg["To"] = "me@example.com"
    msg["Subject"] = "Invoice + specs"

    msg.attach(MIMEText("Hi - invoice and the spec sheet are attached.", "plain"))

    csv_body = "item,qty,price\nwidget,3,19.99\ngizmo,1,42.00\n"
    csv_part = MIMEText(csv_body, "csv")
    csv_part.add_header("Content-Disposition", "attachment", filename="invoice.csv")
    msg.attach(csv_part)

    pdf_part = MIMEApplication(b"%PDF-1.4 fake pdf bytes for the offline check", _subtype="pdf")
    pdf_part.add_header("Content-Disposition", "attachment", filename="spec-sheet.pdf")
    msg.attach(pdf_part)

    huge_text = "x" * (MAX_TEXT_SOURCE_BYTES + 5000)
    huge_part = MIMEText(huge_text, "plain")
    huge_part.add_header("Content-Disposition", "attachment", filename="giant-log.txt")
    msg.attach(huge_part)

    # A part with a filename but no explicit Content-Disposition at all -
    # should still count as an attachment (the old-style name= param on
    # Content-Type is get_filename()'s documented fallback).
    inline_part = MIMEText("# Notes\n\nSee section 2.", "markdown")
    inline_part.set_param("name", "notes.md")
    msg.attach(inline_part)

    atts = extract_attachments(msg)
    by_name = {a["name"]: a for a in atts}

    _check("finds exactly 4 attachments (body excluded)", len(atts) == 4)
    _check("csv is inlined (text present) and content matches",
           by_name.get("invoice.csv", {}).get("text", "").startswith("item,qty,price"))
    _check("csv size is reported and > 0",
           by_name.get("invoice.csv", {}).get("size", 0) > 0)
    _check("pdf is named-only (text is None)",
           by_name.get("spec-sheet.pdf", {}).get("text") is None)
    _check("pdf content_type recorded", by_name.get("spec-sheet.pdf", {}).get("content_type") == "application/pdf")
    _check("oversized text/plain (>100KB) is capped OUT (text is None despite being text/plain)",
           by_name.get("giant-log.txt", {}).get("text") is None
           and by_name.get("giant-log.txt", {}).get("size", 0) > MAX_TEXT_SOURCE_BYTES)
    _check("a filename with no Content-Disposition header still counts as an attachment",
           "notes.md" in by_name)
    _check("markdown attachment is inlined", (by_name.get("notes.md", {}).get("text") or "").startswith("# Notes"))

    # ---- char cap on an in-bounds-by-bytes but long text excerpt
    long_csv = "col\n" + "\n".join(f"row{i},value{i}" for i in range(2000))  # well under 100KB, over 4000 chars
    assert len(long_csv.encode()) <= MAX_TEXT_SOURCE_BYTES
    assert len(long_csv) > MAX_TEXT_CHARS
    msg2 = MIMEMultipart()
    msg2.attach(MIMEText("body", "plain"))
    long_part = MIMEText(long_csv, "csv")
    long_part.add_header("Content-Disposition", "attachment", filename="long.csv")
    msg2.attach(long_part)
    atts2 = extract_attachments(msg2)
    long_text = next(a for a in atts2 if a["name"] == "long.csv")["text"]
    _check("text excerpt is capped at MAX_TEXT_CHARS even when the source is under the byte cap",
           len(long_text) == MAX_TEXT_CHARS)

    # ---- no attachments at all
    msg3 = MIMEMultipart()
    msg3.attach(MIMEText("just a plain reply, nothing attached", "plain"))
    _check("a message with only a body has zero attachments", extract_attachments(msg3) == [])

    # ---- a genuinely single-part (non-multipart) message
    bare = MIMEText("single part, no attachments possible", "plain")
    _check("a non-multipart message returns []", extract_attachments(bare) == [])

    # ---- attachment_context()
    _check("attachment_context('') for no attachments", attachment_context([]) == "")
    ctx = attachment_context(atts)
    _check("readable attachment is inlined with its content in the context paragraph",
           "Attachment invoice.csv contains: item,qty,price" in ctx)
    _check("unreadable attachment gets the explicit 'have NOT read' instruction",
           "You have NOT read spec-sheet.pdf (application/pdf)" in ctx
           and "Do not claim or imply you read it" in ctx)
    _check("oversized text attachment is treated as unreadable in the context too",
           "You have NOT read giant-log.txt" in ctx)

    print(f"\n{len(_fail)} failed" if _fail else "\nAll attachments.py checks passed.")
    _sys.exit(1 if _fail else 0)
