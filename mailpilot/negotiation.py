"""Negotiation Copilot — three reply stances instead of one.

Some inbound emails are not a normal message to answer, they're an opening
move: a rate pushback, a scope-creep ask, a "can you do it for X instead".
For those, one drafted reply forces a premature choice. This module asks the
same model ONE call for THREE stances instead - anchor high, meet in the
middle, walk away - and lets the human pick.

Output format is deliberately NOT JSON. A model asked for JSON inside a long
prose reply routinely closes a brace wrong, quotes a smart quote unescaped,
or wraps the object in a markdown fence it forgets to open/close - and a
negotiation reply is exactly the kind of text (dollar signs, "don't",
nested quotes) that breaks a naive JSON parse. A flat, line-oriented marker
format degrades gracefully: even a mangled response usually still has its
===BODY=== markers intact because the model is copying a literal string it
was shown, not generating balanced syntax.

Two independent entry points:
- draft_negotiation()       - the explicit "negotiate this" action (one call,
                               parsed strictly, raises on a broken format).
- NEGOTIATION_AUTODETECT_ADDENDUM + detect_stance_output() - for
  negotiation_autodetect: appended to the NORMAL single-reply system prompt
  so the everyday drafting call can itself decide to emit the three-stance
  format; detect_stance_output() tells the poller which shape came back,
  returning None (not raising) so a normal single reply is never mistaken
  for a broken negotiation response.
"""
import re

try:
    from . import drafter
except ImportError:
    # Standalone execution (e.g. `python negotiation.py` for the offline
    # checks below, before this file has been dropped into the mailpilot/
    # package per INTEGRATION.md) - fall back to an absolute import against
    # the real install. Once this module lives at mailpilot/negotiation.py
    # and is imported normally, the relative import above always wins and
    # this branch never runs.
    import sys as _sys
    if "/home/ed/mailpilot" not in _sys.path:
        _sys.path.insert(0, "/home/ed/mailpilot")
    from mailpilot import drafter

STANCES = ("anchor", "middle", "walk")

_DEFAULT_LABELS = {
    "anchor": "Anchor high",
    "middle": "Meet in the middle",
    "walk": "Walk away",
}

# ------------------------------------------------------------- prompt text

_FORMAT_BLOCK = """When you draft a negotiation reply, output EXACTLY three stances - and
NOTHING else, no preamble, no commentary, no markdown fences - using this
exact plain-text marker format:

===STANCE anchor===
===LABEL===
A short label for this stance (e.g. "Anchor high")
===RATIONALE===
1-3 sentences of internal reasoning for {signature} - why this stance, what
it risks. This is NEVER sent to the other party.
===BODY===
The complete, ready-to-send reply body for this stance. Plain text only -
no subject line, no quoted original, no markdown.

===STANCE middle===
===LABEL===
A short label (e.g. "Meet in the middle")
===RATIONALE===
...
===BODY===
...

===STANCE walk===
===LABEL===
A short label (e.g. "Polite decline")
===RATIONALE===
...
===BODY===
...

Rules for the three stances:
- anchor: favorable to {signature} but still defensible and reasonable -
  a real opening position, not an insulting one.
- middle: a genuine compromise either side could plausibly accept.
- walk: politely decline or walk away from this deal. Professional, leaves
  the door open only where that's genuinely appropriate - don't manufacture
  warmth the anchor/middle stances didn't earn.
- Every BODY still follows the reply rules given earlier in this prompt
  (only answer what was actually asked, never invent facts/prices/dates,
  sign off as {signature}).
- Use these markers verbatim, in this exact order (anchor, then middle,
  then walk). Do not add a fourth stance, and do not omit one."""


def _format_block(cfg: dict) -> str:
    return _FORMAT_BLOCK.format(signature=cfg.get("signature_name") or "you")


def build_negotiation_system_prompt(cfg: dict, samples=None) -> str:
    """The normal drafting system prompt, plus the three-stance format spec.
    Used for the explicit "Negotiate" action - every call through this
    prompt is expected to come back in stance format, no auto-detection."""
    base = drafter.build_system_prompt(cfg, samples=samples)
    return base + "\n\n" + _format_block(cfg)


NEGOTIATION_AUTODETECT_ADDENDUM_TEMPLATE = """Before drafting, check whether this email is SUBSTANTIVELY a negotiation -
the sender is countering an offer, asking for a discount or a different
rate, pushing back on scope or timeline, or otherwise bargaining over
price, rate, scope, or terms (not just mentioning a price in passing).

If it IS a negotiation: do NOT draft a single reply. Instead output the
three-stance format below instead of your normal reply, using these exact
markers and nothing else:

{format_block}

If it is NOT a negotiation, ignore everything above and draft your normal
single plain-text reply exactly as instructed earlier in this prompt."""


def negotiation_autodetect_addendum(cfg: dict) -> str:
    """The paragraph the integrator appends to the normal drafting system
    prompt (build_system_prompt's output) when cfg['negotiation_autodetect']
    is on. Keeping this as a function (not a bare constant) so the format
    block picks up the real signature name, same as the explicit path."""
    return NEGOTIATION_AUTODETECT_ADDENDUM_TEMPLATE.format(format_block=_format_block(cfg))


# Bare-constant form for callers that just want the generic text (e.g. to
# show in a Settings info popover) without a cfg to hand - signature reads
# "you" in this form.
NEGOTIATION_AUTODETECT_ADDENDUM = NEGOTIATION_AUTODETECT_ADDENDUM_TEMPLATE.format(
    format_block=_FORMAT_BLOCK.format(signature="you")
)


def build_negotiation_user_prompt(email_row, context: str = "") -> str:
    base = drafter.build_user_prompt(email_row, context=context)
    return base + (
        "\n\n---\nThis is a negotiation. Output the three-stance format "
        "specified in the system prompt (anchor / middle / walk) - not a "
        "single reply."
    )


# ------------------------------------------------------------------ parser

class NegotiationFormatError(ValueError):
    """Raised when a model response doesn't match the three-stance marker
    format. Carries a snippet of the raw text so the caller can log/surface
    something a human can actually debug against."""


_STANCE_RE = re.compile(r"===\s*STANCE\s+(\w+)\s*===", re.IGNORECASE)
_FIELD_RE = re.compile(r"===\s*(LABEL|RATIONALE|BODY)\s*===", re.IGNORECASE)


def _snippet(text: str, n: int = 240) -> str:
    text = (text or "").strip()
    return text[:n] + ("…" if len(text) > n else "")


def parse_stance_output(text: str) -> dict:
    """Strict parser: raises NegotiationFormatError with a clear, specific
    reason (and a snippet of the offending text) on anything malformed.
    Returns {"stances": [{"stance","label","rationale","body"}, ...]} with
    entries always in canonical order (anchor, middle, walk) regardless of
    the order the model emitted them in."""
    text = text or ""
    stance_matches = list(_STANCE_RE.finditer(text))
    if not stance_matches:
        raise NegotiationFormatError(
            "No '===STANCE <name>===' markers found in the response. "
            f"Got: {_snippet(text)}"
        )

    blocks: dict[str, str] = {}
    for i, m in enumerate(stance_matches):
        name = m.group(1).strip().lower()
        start = m.end()
        end = stance_matches[i + 1].start() if i + 1 < len(stance_matches) else len(text)
        if name in blocks:
            raise NegotiationFormatError(f"Stance '{name}' appears more than once in the response.")
        blocks[name] = text[start:end]

    missing = [s for s in STANCES if s not in blocks]
    if missing:
        raise NegotiationFormatError(
            f"Missing stance section(s): {', '.join(missing)}. "
            f"Found: {', '.join(blocks.keys()) or '(none)'}."
        )
    extra = [s for s in blocks if s not in STANCES]
    if extra:
        raise NegotiationFormatError(
            f"Unrecognized stance name(s): {', '.join(extra)} "
            f"(expected only {', '.join(STANCES)})."
        )

    stances = []
    for name in STANCES:  # canonical order for display, independent of source order
        block = blocks[name]
        field_matches = list(_FIELD_RE.finditer(block))
        if not field_matches:
            raise NegotiationFormatError(
                f"Stance '{name}' has no ===LABEL===/===RATIONALE===/===BODY=== "
                f"fields. Got: {_snippet(block)}"
            )
        fields: dict[str, str] = {}
        for i, fm in enumerate(field_matches):
            fname = fm.group(1).strip().lower()
            fstart = fm.end()
            fend = field_matches[i + 1].start() if i + 1 < len(field_matches) else len(block)
            fields[fname] = block[fstart:fend].strip()

        for required in ("label", "rationale", "body"):
            if required not in fields:
                raise NegotiationFormatError(
                    f"Stance '{name}' is missing its ==={required.upper()}=== field."
                )
            if not fields[required]:
                raise NegotiationFormatError(
                    f"Stance '{name}' has an empty {required.upper()} - "
                    f"the response looks truncated. Got: {_snippet(block)}"
                )

        stances.append({
            "stance": name,
            "label": fields["label"],
            "rationale": fields["rationale"],
            "body": fields["body"],
        })

    return {"stances": stances}


def detect_stance_output(text: str):
    """Non-raising cousin of parse_stance_output(), for the autodetect path
    where a response might legitimately be an ordinary single reply. Returns
    the parsed stances dict, or None if `text` doesn't parse as one (a plain
    reply, or a genuinely malformed negotiation attempt - the poller treats
    both the same way: fall back to storing it as a normal single reply)."""
    if not text or "===STANCE" not in text.upper():
        return None
    try:
        return parse_stance_output(text)
    except NegotiationFormatError:
        return None


# -------------------------------------------------------------- entry point

def draft_negotiation(email_row, cfg: dict = None, generate_fn=None, db_file=None) -> dict:
    """The explicit "Negotiate" action. Calls the model ONCE and returns the
    parsed three-stance dict, or raises NegotiationFormatError /
    whatever the underlying generate call raises (RuntimeError from
    drafter._draft_via_api/_draft_via_cli) - callers should handle both the
    way server.py already handles drafter exceptions (record_error + a 502).

    generate_fn(system_prompt, user_prompt, cfg) -> str lets tests (and any
    future caller) skip the real Claude call; defaults to drafter.generate.
    """
    cfg = cfg or {}
    generate_fn = generate_fn or drafter.generate

    context = ""
    try:
        context = drafter._sender_context(email_row, db_file)
    except Exception:
        pass  # thread memory is a nice-to-have, never a hard dependency

    system_prompt = build_negotiation_system_prompt(cfg)
    user_prompt = build_negotiation_user_prompt(email_row, context=context)
    raw = generate_fn(system_prompt, user_prompt, cfg)
    return parse_stance_output(raw)


# ------------------------------------------------------------------ offline

if __name__ == "__main__":
    import sys as _sys

    _fail = []

    def _check(label, cond):
        print(("PASS " if cond else "FAIL ") + label)
        if not cond:
            _fail.append(label)

    _cfg = {"signature_name": "Ed", "gmail_address": "ed@example.com"}
    _email = {
        "id": 1, "from_name": "Alex Buyer", "from_address": "alex@example.com",
        "subject": "Re: your rate", "received_at": "2026-09-01T09:00:00-04:00",
        "body_text": "Can you do this for $2,000 instead of $3,500?",
    }

    def _well_formed_text():
        return (
            "===STANCE anchor===\n"
            "===LABEL===\nAnchor high\n"
            "===RATIONALE===\nStart above target, we have room to move.\n"
            "===BODY===\nHi Alex,\n\nI can do this at $3,200 given the scope.\n\nEd\n"
            "\n===STANCE middle===\n"
            "===LABEL===\nMeet in the middle\n"
            "===RATIONALE===\nA fair midpoint that still covers cost.\n"
            "===BODY===\nHi Alex,\n\nLet's land at $2,750.\n\nEd\n"
            "\n===STANCE walk===\n"
            "===LABEL===\nPolite decline\n"
            "===RATIONALE===\n$2,000 doesn't cover the work; better to pass.\n"
            "===BODY===\nHi Alex,\n\n$2,000 doesn't work on our end this time.\n\nEd\n"
        )

    def _well_formed_generate(system_prompt, user_prompt, cfg):
        assert "===STANCE anchor===" in system_prompt
        assert "negotiation" in user_prompt.lower()
        return _well_formed_text()

    result = draft_negotiation(_email, _cfg, generate_fn=_well_formed_generate)
    _check("well-formed round-trip returns 3 stances", len(result["stances"]) == 3)
    _check("stance order is anchor/middle/walk",
           [s["stance"] for s in result["stances"]] == ["anchor", "middle", "walk"])
    _check("middle body carries the compromise number",
           "2,750" in result["stances"][1]["body"])
    _check("rationale never leaks into body",
           "cover cost" not in result["stances"][1]["body"])

    def _reordered_generate(system_prompt, user_prompt, cfg):
        return (
            "===STANCE walk===\n===LABEL===\nNo\n===RATIONALE===\nr\n===BODY===\nb-walk\n"
            "===STANCE anchor===\n===LABEL===\nHi\n===RATIONALE===\nr\n===BODY===\nb-anchor\n"
            "===STANCE middle===\n===LABEL===\nMid\n===RATIONALE===\nr\n===BODY===\nb-middle\n"
        )

    reordered = draft_negotiation(_email, _cfg, generate_fn=_reordered_generate)
    _check("out-of-order source still normalizes to canonical order",
           [s["stance"] for s in reordered["stances"]] == ["anchor", "middle", "walk"])

    def _malformed_missing_marker(system_prompt, user_prompt, cfg):
        return "Sure, here's a reply: I can do $2,900. Let me know!"

    try:
        draft_negotiation(_email, _cfg, generate_fn=_malformed_missing_marker)
        _check("malformed (no markers) raises NegotiationFormatError", False)
    except NegotiationFormatError:
        _check("malformed (no markers) raises NegotiationFormatError", True)

    def _malformed_missing_stance(system_prompt, user_prompt, cfg):
        return (
            "===STANCE anchor===\n===LABEL===\nA\n===RATIONALE===\nr\n===BODY===\nb\n"
            "===STANCE middle===\n===LABEL===\nM\n===RATIONALE===\nr\n===BODY===\nb\n"
        )  # walk missing entirely

    try:
        draft_negotiation(_email, _cfg, generate_fn=_malformed_missing_stance)
        _check("malformed (missing 'walk' stance) raises", False)
    except NegotiationFormatError as e:
        _check("malformed (missing 'walk' stance) raises", "walk" in str(e))

    def _malformed_empty_body(system_prompt, user_prompt, cfg):
        return (
            "===STANCE anchor===\n===LABEL===\nA\n===RATIONALE===\nr\n===BODY===\n\n"
            "===STANCE middle===\n===LABEL===\nM\n===RATIONALE===\nr\n===BODY===\nb\n"
            "===STANCE walk===\n===LABEL===\nW\n===RATIONALE===\nr\n===BODY===\nb\n"
        )

    try:
        draft_negotiation(_email, _cfg, generate_fn=_malformed_empty_body)
        _check("malformed (empty anchor BODY) raises", False)
    except NegotiationFormatError as e:
        _check("malformed (empty anchor BODY) raises", "anchor" in str(e).lower())

    # detect_stance_output: the autodetect fork of the same parser
    ok_text = _well_formed_text()
    detected = detect_stance_output(ok_text)
    _check("detect_stance_output positive returns a parsed dict",
           detected is not None and len(detected["stances"]) == 3)

    plain_reply = "Hi Alex,\n\nSure, $2,900 works for me. Let's do it.\n\nEd"
    _check("detect_stance_output negative (plain reply) returns None",
           detect_stance_output(plain_reply) is None)

    garbled = "===STANCE anchor===\nI got confused and just wrote prose here."
    _check("detect_stance_output negative (garbled attempt) returns None, not raise",
           detect_stance_output(garbled) is None)

    _check("negotiation_autodetect_addendum embeds the real signature",
           "Ed" in negotiation_autodetect_addendum(_cfg))
    _check("bare NEGOTIATION_AUTODETECT_ADDENDUM constant is non-empty",
           "===STANCE anchor===" in NEGOTIATION_AUTODETECT_ADDENDUM)

    print(f"\n{len(_fail)} failed" if _fail else "\nAll negotiation.py checks passed.")
    _sys.exit(1 if _fail else 0)
