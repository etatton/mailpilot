"""Drafting replies with Claude — two interchangeable brains.

"api" mode: the `anthropic` SDK with the user's own API key.
"cli" mode: the locally installed, already-signed-in Claude Code CLI, driven as
a subprocess in print mode. The app never performs any Anthropic login itself —
in cli mode it only uses credentials the user's own Claude Code install manages.
"""
import json
import shutil
import subprocess
import sys
from pathlib import Path

from . import config

FIXED_RULES = (
    "Rules for the reply:\n"
    "- Reply only to what the email actually asks.\n"
    "- Never invent facts, prices, dates, or commitments. Where needed "
    "information is missing, leave an explicit [FILL IN: what's needed] placeholder.\n"
    "- Match the sender's level of formality.\n"
    "- Sign off as {signature}.\n"
    "- Output ONLY the reply body as plain text - no subject line, no quoted "
    "original, no preamble, no markdown."
)


def _load_samples(db_file=None) -> list[str]:
    from . import db
    try:
        with db.conn(db_file) as c:
            rows = c.execute(
                "SELECT body FROM voice_samples ORDER BY id DESC LIMIT 10"
            ).fetchall()
        return [r["body"] for r in rows]
    except Exception:
        return []


def build_system_prompt(cfg: dict, samples: list[str] | None = None) -> str:
    parts = [
        f"You draft email replies on behalf of {cfg['signature_name']} <{cfg['gmail_address']}>."
    ]
    if cfg.get("tone_notes"):
        parts.append("Style guidance from the user: " + cfg["tone_notes"])
    profile = (cfg.get("voice_profile") or "").strip()
    if profile:
        parts.append("Voice profile - how this person writes; follow it closely:\n" + profile)
    samples = samples if samples is not None else _load_samples()
    if samples:
        # With a profile, one short exemplar is enough; without, give more raw material
        budget = 1500 if profile else 6000
        joined = ""
        for s in samples:
            s = (s or "").strip()[:3000]
            if not s or len(joined) + len(s) > budget:
                continue
            joined += "\n\n--- SAMPLE ---\n" + s
        if joined:
            parts.append(
                "Real emails the user has written. Match their voice, rhythm, and sign-off:"
                + joined
            )
    parts.append(FIXED_RULES.format(signature=cfg["signature_name"]))
    return "\n\n".join(parts)


def _sender_context(email_row, db_file=None) -> str:
    """Thread memory: recent correspondence with this sender (their earlier
    emails + our sent replies), plus any saved notes about them."""
    from . import db
    parts = []
    try:
        from .poller import lookup_contact
        contact = lookup_contact(email_row["from_address"], db_file)
        if contact and (contact["notes"] or contact["name"]):
            who = contact["name"] or email_row["from_address"]
            note = f" Notes: {contact['notes']}" if contact["notes"] else ""
            parts.append(f"About this person: {who}.{note}")
    except Exception:
        pass
    try:
        with db.conn(db_file) as c:
            prior = c.execute(
                "SELECT e.received_at, e.subject, e.body_text,"
                " (SELECT d.body FROM drafts d WHERE d.email_id=e.id"
                "   AND d.status='sent' ORDER BY d.id DESC LIMIT 1) AS our_reply"
                " FROM emails e WHERE lower(e.from_address)=lower(?) AND e.id != ?"
                " ORDER BY e.id DESC LIMIT 3",
                (email_row["from_address"], email_row["id"]),
            ).fetchall()
        if prior:
            lines = ["Recent correspondence with this person (newest first):"]
            for p in prior:
                lines.append(
                    f"[{p['received_at']}] They wrote ({p['subject'] or 'no subject'}): "
                    + (p["body_text"] or "")[:600]
                )
                if p["our_reply"]:
                    lines.append("You replied: " + p["our_reply"][:600])
            parts.append("\n".join(lines))
    except Exception:
        pass
    return "\n\n".join(parts)


def build_user_prompt(email_row, guidance: str = "", previous_draft: str = "",
                      context: str = "") -> str:
    p = "Draft a reply to this email.\n\n"
    if context:
        p += context + "\n\n---\nThe email to reply to:\n\n"
    p += (
        f"From: {email_row['from_name']} <{email_row['from_address']}>\n"
        f"Subject: {email_row['subject']}\n"
        f"Date: {email_row['received_at']}\n\n"
        f"{email_row['body_text']}"
    )
    if previous_draft:
        p += f"\n\n---\nPrevious draft:\n{previous_draft}"
    if guidance:
        p += f"\n\nRevise the draft according to this instruction: {guidance}"
    return p


# ---------------------------------------------------------------- api mode

def _draft_via_api(system_prompt: str, user_prompt: str, cfg: dict) -> str:
    import anthropic

    key = config.get_secret("anthropic_api_key")
    if not key:
        raise RuntimeError("No Anthropic API key is stored. Open Settings to add one.")
    client = anthropic.Anthropic(api_key=key)
    try:
        resp = client.messages.create(
            model=cfg.get("model") or "claude-opus-5",
            max_tokens=2000,
            system=system_prompt,
            messages=[{"role": "user", "content": user_prompt}],
        )
    except anthropic.AuthenticationError:
        raise RuntimeError("Anthropic rejected the API key (authentication failed). Update it in Settings.")
    except anthropic.RateLimitError:
        raise RuntimeError("Anthropic rate limit hit - will retry on a later cycle.")
    except anthropic.APIStatusError as e:
        raise RuntimeError(f"Anthropic API error {e.status_code}: {e.message}")
    except anthropic.APIConnectionError:
        raise RuntimeError("Could not reach the Anthropic API (network error).")
    if resp.stop_reason == "refusal":
        raise RuntimeError("Claude declined to draft this reply (safety refusal).")
    text = "".join(b.text for b in resp.content if b.type == "text").strip()
    if not text:
        raise RuntimeError("Claude returned an empty draft.")
    return text


def validate_api_key(key: str) -> tuple[bool, str]:
    """Cheapest real check: list models (free, authenticated)."""
    import anthropic
    try:
        anthropic.Anthropic(api_key=key).models.list()
        return True, "API key works."
    except anthropic.AuthenticationError:
        return False, "That API key was rejected. Check for missing characters."
    except anthropic.APIConnectionError:
        return False, "Could not reach the Anthropic API - check your internet connection."
    except Exception as e:
        return False, f"Unexpected error validating the key: {e.__class__.__name__}"


# ---------------------------------------------------------------- cli mode

_CLI_CANDIDATES = [
    Path.home() / ".local" / "bin" / "claude",
    Path.home() / ".local" / "bin" / "claude.exe",
    Path("/usr/local/bin/claude"),
    Path("/opt/homebrew/bin/claude"),
]


def find_claude_cli() -> str:
    hit = shutil.which("claude")
    if hit:
        return hit
    for p in _CLI_CANDIDATES:
        if p.exists():
            return str(p)
    return ""


def _draft_via_cli(system_prompt: str, user_prompt: str, cfg: dict) -> str:
    cli = cfg.get("claude_cli_path") or find_claude_cli()
    if not cli:
        raise RuntimeError(
            "Claude Code isn't installed (or wasn't found). Install it and sign in, "
            "or switch to API-key mode in Settings."
        )
    prompt = (
        "<instructions>\n" + system_prompt + "\n</instructions>\n\n" + user_prompt
    )
    kwargs = {}
    if sys.platform == "win32":
        kwargs["creationflags"] = 0x08000000  # CREATE_NO_WINDOW
    try:
        proc = subprocess.run(
            [cli, "-p", prompt, "--output-format", "json"],
            capture_output=True, text=True, timeout=300, **kwargs,
        )
    except subprocess.TimeoutExpired:
        raise RuntimeError("Claude Code took longer than 5 minutes - will retry later.")
    except FileNotFoundError:
        raise RuntimeError(f"Claude Code executable vanished from {cli}. Re-detect it in Settings.")
    if proc.returncode != 0:
        err = (proc.stderr or proc.stdout or "").strip()[:400]
        low = err.lower()
        if "login" in low or "expired" in low or "auth" in low:
            raise RuntimeError(
                "Claude Code needs to be signed in again. Open a terminal, run `claude`, "
                "and complete the login - drafting resumes automatically."
            )
        raise RuntimeError(f"Claude Code failed (exit {proc.returncode}): {err}")
    try:
        payload = json.loads(proc.stdout)
        text = (payload.get("result") or "").strip()
    except json.JSONDecodeError:
        text = proc.stdout.strip()
    if not text:
        raise RuntimeError("Claude Code returned an empty draft.")
    return text


# ---------------------------------------------------------------- entry points

def generate(system_prompt: str, user_prompt: str, cfg: dict) -> str:
    if cfg.get("mode") == "cli":
        return _draft_via_cli(system_prompt, user_prompt, cfg)
    return _draft_via_api(system_prompt, user_prompt, cfg)


def draft_reply(email_row, cfg: dict = None, guidance: str = "", previous_draft: str = "",
                db_file=None, extra_context: str = "") -> str:
    cfg = cfg or config.load()
    context = _sender_context(email_row, db_file)
    if extra_context:
        context = (context + "\n\n" + extra_context) if context else extra_context

    system_prompt = build_system_prompt(cfg)
    # Autodetect rides the initial draft only: a Regenerate (always carries
    # guidance) keeps producing a normal single reply.
    if (cfg.get("negotiation_autodetect") and cfg.get("feature_negotiation")
            and not guidance):
        from . import negotiation
        system_prompt += "\n\n" + negotiation.negotiation_autodetect_addendum(cfg)

    return generate(
        system_prompt,
        build_user_prompt(email_row, guidance, previous_draft, context=context),
        cfg,
    )


def draft_followup(email_row, sent_reply: str, days: int, cfg: dict = None) -> str:
    """A short nudge on a reply that got no response. Queued like any draft."""
    cfg = cfg or config.load()
    user = (
        f"You replied to this person {days} days ago and they haven't responded. "
        "Draft a SHORT, friendly follow-up nudge (2-4 sentences): reference your "
        "earlier reply naturally, no guilt-tripping, make it easy to answer.\n\n"
        f"Their original email (From: {email_row['from_name']} "
        f"<{email_row['from_address']}>, Subject: {email_row['subject']}):\n"
        f"{(email_row['body_text'] or '')[:1500]}\n\n"
        f"Your reply that went unanswered:\n{(sent_reply or '')[:1500]}"
    )
    return generate(build_system_prompt(cfg), user, cfg)


def analyze_voice(cfg: dict = None, db_file=None) -> str:
    """Distill the stored writing samples into a compact, editable voice profile."""
    cfg = cfg or config.load()
    samples = _load_samples(db_file)
    if not samples:
        raise RuntimeError("Add at least one writing sample first.")
    joined = ""
    for s in samples:
        if len(joined) > 15000:
            break
        joined += "\n\n--- SAMPLE ---\n" + s.strip()[:4000]
    system = (
        "You analyze how a person writes email and produce a compact voice profile "
        "another writer can follow to imitate them."
    )
    user = (
        "Here are emails written by one person. Write their voice profile in under 180 "
        "words: typical greeting and sign-off, sentence length and rhythm, formality, "
        "warmth, punctuation and formatting habits, characteristic words or phrases, and "
        "anything they never do. Output ONLY the profile, as short plain-text bullet lines."
        + joined
    )
    return generate(system, user, cfg).strip()
