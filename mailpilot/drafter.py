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


def build_system_prompt(cfg: dict) -> str:
    parts = [
        f"You draft email replies on behalf of {cfg['signature_name']} <{cfg['gmail_address']}>."
    ]
    if cfg.get("tone_notes"):
        parts.append("Style guidance from the user: " + cfg["tone_notes"])
    parts.append(FIXED_RULES.format(signature=cfg["signature_name"]))
    return "\n\n".join(parts)


def build_user_prompt(email_row, guidance: str = "", previous_draft: str = "") -> str:
    p = (
        "Draft a reply to this email.\n\n"
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


# ---------------------------------------------------------------- entry point

def draft_reply(email_row, cfg: dict = None, guidance: str = "", previous_draft: str = "") -> str:
    cfg = cfg or config.load()
    system_prompt = build_system_prompt(cfg)
    user_prompt = build_user_prompt(email_row, guidance, previous_draft)
    if cfg.get("mode") == "cli":
        return _draft_via_cli(system_prompt, user_prompt, cfg)
    return _draft_via_api(system_prompt, user_prompt, cfg)
