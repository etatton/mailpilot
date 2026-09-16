"""Parley — two MailPilots negotiating a meeting time, one approval per round.

A parley email is a NORMAL email. The human-readable body says what any polite
scheduling mail says ("Would any of these work? Tue 2pm, Wed 10am…"); a small
machine-readable block rides at the very end of the same plain-text body,
between the exact markers ``[parley-data]`` and ``[/parley-data]``, under a
line that tells a human reader what it is. A recipient who has never heard of
MailPilot sees a normal email with a short data footer.

Why a body block and not a MIME part: ``sender.send_reply`` builds the message
with ``msg.set_content(body)`` on plain text. A body block survives that path
untouched, so Parley needs ZERO changes to the send choke-point — the block is
just characters in the body the user approves. Outbound mail also carries the
header ``X-Parley: v1`` (see INTEGRATION.md), but the block is what carries the
data; the header is a cheap fast-path hint.

Nothing here sends anything. Every round this module produces is a draft in the
queue, approved by a human, sent by the existing choke-point. v1 scope is
scheduling only (``kind: "schedule"``), capped at MAX_ROUNDS rounds — after
that the module stops responding and hands the thread to the human.

Public surface:
    detect(msg_or_headers, body_text)   -> parley dict | None
    strip_block(body_text)              -> body without the data block
    build_block(data)                   -> marker-wrapped JSON string
    parse_availability(text)            -> availability structure
    propose_slots(availability, n, tz)  -> [slot, …]
    respond(data, availability, email_row, cfg) -> (body, new_data) | (None, None)
    start_parley(email_row, availability, cfg)  -> body

Helpers the integrator needs: render_body, strip_quoted, is_echo,
handoff_reason, slot_fits, PARLEY_HEADER, PARLEY_HEADER_VALUE, MAX_ROUNDS.
"""
import json
import re
from datetime import date as _date
from datetime import datetime as _datetime
from datetime import timedelta

try:                                     # normal in-package import
    from . import drafter
except ImportError:                      # running this file directly (the checks below)
    drafter = None

# ---------------------------------------------------------------- constants

PROTOCOL_VERSION = 1
PARLEY_HEADER = "X-Parley"
PARLEY_HEADER_VALUE = "v1"
OPEN_MARKER = "[parley-data]"
CLOSE_MARKER = "[/parley-data]"
BLOCK_LABEL = "(structured data for scheduling assistants)"
MAX_ROUNDS = 3                 # a counter that would be round 4 is handed to the human
DEFAULT_DURATION = 30          # minutes
SLOT_STRIDE = 120              # minutes between candidate times inside one window
HORIZON_DAYS = 14              # how far ahead propose_slots will look
STATES = ("propose", "counter", "accept")

_BLOCK_RE = re.compile(
    re.escape(OPEN_MARKER) + r"(.*?)" + re.escape(CLOSE_MARKER), re.S
)
_LABEL_RE = re.compile(r"^[ \t]*" + re.escape(BLOCK_LABEL) + r"[ \t]*$", re.M)
_TRAILING_SEP_RE = re.compile(r"\n[ \t]*--[ \t]*\n?\s*$")
_QUOTE_INTRO_RE = re.compile(
    r"^\s*(On .{0,200}\bwrote:|-{2,}\s*Original Message\s*-{2,}|_{5,})\s*$", re.M | re.I
)


# ---------------------------------------------------------------- the block

def build_block(data: dict) -> str:
    """The marker-wrapped JSON string, exactly as it appears in a body.

    Emits exactly the six fixed keys, in order, one slot per line: short enough
    that ``set_content`` keeps the whole message 7bit (no quoted-printable soft
    wraps to un-mangle at the far end) and compact enough that a human who
    scrolls down sees a few readable lines, not a page of JSON. If any line
    would still run past the 78-column line limit, it falls back to plain
    ``indent=2`` — correctness over tidiness.
    """
    payload = {
        "v": PROTOCOL_VERSION,
        "kind": "schedule",
        "state": data.get("state") or "propose",
        "round": int(data.get("round") or 1),
        "slots": [_slot_out(s) for s in (data.get("slots") or [])],
        "chosen": _slot_out(data["chosen"]) if data.get("chosen") else None,
    }
    dump = lambda o: json.dumps(o, ensure_ascii=False)   # noqa: E731
    slots = payload["slots"]
    inner = "[]" if not slots else "[\n    " + ",\n    ".join(dump(s) for s in slots) + "\n  ]"
    text = (
        "{" + f'"v": {payload["v"]}, "kind": "schedule", '
        f'"state": {dump(payload["state"])}, "round": {payload["round"]},'
        f'\n  "slots": {inner},'
        f'\n  "chosen": {dump(payload["chosen"])}\n' + "}"
    )
    if max((len(ln) for ln in text.splitlines()), default=0) > 78:
        text = json.dumps(payload, indent=2, ensure_ascii=False)
    return OPEN_MARKER + "\n" + text + "\n" + CLOSE_MARKER


def render_body(sentence: str, data: dict) -> str:
    """Human text + labelled block. The block is the LAST thing in the body."""
    return (sentence or "").rstrip() + "\n\n--\n" + BLOCK_LABEL + "\n" + build_block(data) + "\n"


def strip_block(body_text: str) -> str:
    """Body without the data block — what the UI should show a human."""
    text = _BLOCK_RE.sub("", body_text or "")
    text = _LABEL_RE.sub("", text)
    text = _TRAILING_SEP_RE.sub("", text)
    return re.sub(r"\n{3,}", "\n\n", text).rstrip()


def strip_quoted(body_text: str) -> str:
    """Drop quoted history from a body before detecting.

    A human replying in Gmail quotes our previous message underneath theirs —
    including our own [parley-data] block. Detecting that block would make
    MailPilot negotiate with itself. The poller hook calls detect() on the
    output of this function; MailPilot-to-MailPilot mail is never quoted
    (sender.py sets the draft body as the whole content) so this is a no-op on
    the happy path.
    """
    text = body_text or ""
    m = _QUOTE_INTRO_RE.search(text)
    if m:
        text = text[: m.start()]
    lines = [ln for ln in text.splitlines() if not ln.lstrip().startswith(">")]
    return "\n".join(lines).rstrip()


def _header_present(msg_or_headers) -> bool:
    src = msg_or_headers
    if src is None:
        return False
    try:
        if hasattr(src, "get") and not isinstance(src, dict):     # email.message.Message
            return bool(src.get(PARLEY_HEADER))
        items = src.items() if isinstance(src, dict) else src     # dict or [(k, v), …]
        for k, v in items:
            if str(k).strip().lower() == PARLEY_HEADER.lower() and str(v).strip():
                return True
    except Exception:
        return False
    return False


def detect(msg_or_headers, body_text: str):
    """Parsed parley dict, or None.

    Triggers on the X-Parley header OR the body markers — either is enough to
    look, but the BLOCK is what is actually returned: a header with no parseable
    block yields None, because there is nothing to negotiate with. Malformed
    JSON, a wrong version/kind, or an unknown state all yield None rather than
    raising: an email is never allowed to crash the poller.

    When a body carries several blocks (quoted history that strip_quoted did not
    catch) the LAST one wins — in a reply-below-quote thread that is the newest.
    """
    body = body_text or ""
    has_markers = OPEN_MARKER in body and CLOSE_MARKER in body
    if not (has_markers or _header_present(msg_or_headers)):
        return None
    matches = _BLOCK_RE.findall(body)
    if not matches:
        return None
    raw = matches[-1]
    parsed = _loads_forgiving(raw)
    if parsed is None:
        return None
    return _normalize_data(parsed)


def _loads_forgiving(raw: str):
    for candidate in (
        raw,
        re.sub(r"=\r?\n", "", raw),                                   # undecoded QP soft breaks
        "\n".join(ln.lstrip(">").strip() for ln in raw.splitlines()),  # quote markers
    ):
        try:
            return json.loads(candidate)
        except Exception:
            continue
    return None


def _normalize_data(raw):
    if not isinstance(raw, dict):
        return None
    try:
        if int(raw.get("v", PROTOCOL_VERSION)) != PROTOCOL_VERSION:
            return None
    except (TypeError, ValueError):
        return None
    if (raw.get("kind") or "schedule") != "schedule":
        return None
    state = str(raw.get("state") or "").strip().lower()
    if state not in STATES:
        return None
    try:
        rnd = max(1, int(raw.get("round") or 1))
    except (TypeError, ValueError):
        rnd = 1
    slots = []
    for s in raw.get("slots") or []:
        norm = _slot_in(s)
        if norm:
            slots.append(norm)
    chosen = _slot_in(raw.get("chosen"))
    return {"v": PROTOCOL_VERSION, "kind": "schedule", "state": state,
            "round": rnd, "slots": slots, "chosen": chosen}


# ---------------------------------------------------------------- slots

def _slot_in(s):
    """Tolerant inbound slot -> canonical slot, or None if it can't be trusted.

    A slot without a parseable ISO date or time is dropped: a date we cannot
    place on a calendar can never be checked against availability, and agreeing
    to a time we did not understand is the one failure that reaches a human's
    diary.
    """
    if not isinstance(s, dict):
        return None
    d = _parse_date(s.get("date"))
    t = _parse_time(s.get("time"))
    if d is None or t is None:
        return None
    try:
        dur = int(s.get("dur") or DEFAULT_DURATION)
    except (TypeError, ValueError):
        dur = DEFAULT_DURATION
    dur = max(5, min(dur, 8 * 60))
    return _make_slot(d, t, dur)


def _slot_out(s):
    norm = _slot_in(s) if isinstance(s, dict) else None
    return norm or None


def _make_slot(d: _date, minutes: int, dur: int = DEFAULT_DURATION) -> dict:
    return {"day": d.strftime("%a"), "date": d.isoformat(),
            "time": f"{minutes // 60:02d}:{minutes % 60:02d}", "dur": int(dur)}


def _parse_date(value):
    if isinstance(value, _date):
        return value
    text = str(value or "").strip()[:10]
    try:
        return _datetime.strptime(text, "%Y-%m-%d").date()
    except ValueError:
        return None


_TIME_RE = re.compile(r"^\s*(\d{1,2})(?::(\d{2}))?\s*(am|pm)?\s*$", re.I)


def _parse_time(value):
    """'14:00' | '2pm' | '2:30 PM' | 14 -> minutes since midnight, else None."""
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        v = int(value)
        return v if 0 <= v < 24 * 60 else None
    m = _TIME_RE.match(str(value or ""))
    if not m:
        return None
    hour = int(m.group(1))
    minute = int(m.group(2) or 0)
    suffix = (m.group(3) or "").lower()
    if suffix == "pm" and hour < 12:
        hour += 12
    elif suffix == "am" and hour == 12:
        hour = 0
    if hour > 23 or minute > 59:
        return None
    return hour * 60 + minute


def format_time(minutes: int) -> str:
    h, m = divmod(int(minutes), 60)
    suffix = "am" if h < 12 else "pm"
    h12 = h % 12 or 12
    return f"{h12}:{m:02d}{suffix}" if m else f"{h12}{suffix}"


def format_slot(slot: dict) -> str:
    """'Tue 22 Sep, 2pm (30 min)' — for the prompt and any fallback sentence."""
    s = _slot_in(slot) or {}
    d = _parse_date(s.get("date"))
    when = d.strftime("%a %-d %b") if d else (s.get("day") or "")
    return f"{when}, {format_time(_parse_time(s.get('time')) or 0)} ({s.get('dur', DEFAULT_DURATION)} min)"


# ---------------------------------------------------------------- availability

_DAY_NAMES = {
    "mon": 0, "monday": 0, "tue": 1, "tues": 1, "tuesday": 1,
    "wed": 2, "weds": 2, "wednesday": 2, "thu": 3, "thur": 3, "thurs": 3,
    "thursday": 3, "fri": 4, "friday": 4, "sat": 5, "saturday": 5,
    "sun": 6, "sunday": 6,
}
_DAY_GROUPS = {
    "weekday": [0, 1, 2, 3, 4], "weekdays": [0, 1, 2, 3, 4],
    "weekend": [5, 6], "weekends": [5, 6],
    "daily": [0, 1, 2, 3, 4, 5, 6], "everyday": [0, 1, 2, 3, 4, 5, 6],
}
_PART_OF_DAY = {
    "morning": (9 * 60, 12 * 60), "mornings": (9 * 60, 12 * 60),
    "afternoon": (13 * 60, 17 * 60), "afternoons": (13 * 60, 17 * 60),
    "evening": (18 * 60, 20 * 60), "evenings": (18 * 60, 20 * 60),
    "anytime": (9 * 60, 17 * 60), "allday": (9 * 60, 17 * 60),
}
_DEFAULT_WINDOW = (9 * 60, 17 * 60)

_RANGE_RE = re.compile(
    r"(\d{1,2}(?::\d{2})?\s*(?:am|pm)?)\s*(?:-|–|—|to|until|till)\s*(\d{1,2}(?::\d{2})?\s*(?:am|pm)?)",
    re.I,
)
_AFTER_RE = re.compile(r"\bafter\s+(\d{1,2}(?::\d{2})?\s*(?:am|pm)?)", re.I)
_BEFORE_RE = re.compile(r"\bbefore\s+(\d{1,2}(?::\d{2})?\s*(?:am|pm)?)", re.I)
_DUR_MIN_RE = re.compile(r"(\d{1,3})\s*(?:min\b|mins\b|minutes\b)", re.I)
_DUR_HR_RE = re.compile(r"(\d{1,2})\s*(?:hour\b|hours\b|hr\b|hrs\b)", re.I)
_TZ_RE = re.compile(
    r"\b(?:(?P<iana>[A-Z][A-Za-z]+/[A-Za-z_]+)|(?P<abbr>ET|EST|EDT|CT|CST|CDT|MT|MST|MDT|"
    r"PT|PST|PDT|UTC|GMT|BST|CET|CEST)|(?P<word>Eastern|Central|Mountain|Pacific))\b"
)


def parse_availability(text: str) -> dict:
    """Freeform availability -> {"windows": [...], "dur": int, "tz": str, "raw": str}.

    Grammar (forgiving — an unreadable clause is skipped, never fatal):
      clauses      separated by , ; or newline
      days         Mon/Monday/Tues/Weds… joined by + / & or "and";
                   also weekdays, weekends, daily, every day
      times        "13:00-17:00", "1-5pm", "9am to 12", "after 2pm",
                   "before noon", mornings, afternoons, evenings, all day
      duration     "30 min", "1 hour" anywhere in the text (first wins)
      timezone     "ET", "Eastern", "America/New_York" anywhere (first wins)
      no days      -> weekdays;   no times -> 09:00-17:00

    An input that yields nothing usable falls back to Mon-Fri 09:00-17:00, so a
    blank setting still produces sane proposals rather than silence.
    """
    raw = (text or "").strip()
    dur = DEFAULT_DURATION
    hm = _DUR_HR_RE.search(raw)
    mm = _DUR_MIN_RE.search(raw)
    if mm and (not hm or mm.start() < hm.start()):
        dur = max(5, min(int(mm.group(1)), 8 * 60))
    elif hm:
        dur = max(5, min(int(hm.group(1)) * 60, 8 * 60))
    if re.search(r"\bhalf[ -]?hour\b", raw, re.I):
        dur = 30
    tzm = _TZ_RE.search(raw)
    tz = (tzm.group(0) if tzm else "")

    windows = []
    for clause in re.split(r"[,;\n]+", raw):
        w = _parse_clause(clause)
        if w:
            windows.append(w)
    if not windows:
        windows = [{"days": [0, 1, 2, 3, 4], "start": _DEFAULT_WINDOW[0], "end": _DEFAULT_WINDOW[1]}]
    return {"windows": windows, "dur": dur, "tz": tz, "raw": raw}


def _parse_clause(clause: str):
    text = (clause or "").strip()
    if not text:
        return None
    low = re.sub(r"\band\b", "+", text.lower())
    low = low.replace("every day", "everyday").replace("all day", "allday")

    days = []
    for first, last in re.findall(r"\b([a-z]{3,9})\s*[-–—]\s*([a-z]{3,9})\b", low):
        if first in _DAY_NAMES and last in _DAY_NAMES:   # "Mon-Fri", "Tue-Thu", "Fri-Mon"
            f, l = _DAY_NAMES[first], _DAY_NAMES[last]
            days.extend(range(f, l + 1) if f <= l else list(range(f, 7)) + list(range(0, l + 1)))
    for token in re.findall(r"[a-z]+", low):
        if token in _DAY_GROUPS:
            days.extend(_DAY_GROUPS[token])
        elif token in _DAY_NAMES:
            days.append(_DAY_NAMES[token])
    days = sorted(set(days))

    start = end = None
    m = _RANGE_RE.search(low)
    if m:
        start, end = _parse_time(m.group(1)), _parse_time(m.group(2))
        if start is not None and end is not None:
            left_ampm = re.search(r"(am|pm)", m.group(1), re.I)
            right_ampm = re.search(r"(am|pm)", m.group(2), re.I)
            # "1-5pm": a bare left side inherits the right side's meridiem
            if not left_ampm and right_ampm and right_ampm.group(1).lower() == "pm" \
                    and start < 12 * 60 and start + 12 * 60 < end:
                start += 12 * 60
            # "9-5": a bare range that runs backwards is an afternoon end
            elif end <= start and not right_ampm and end < 12 * 60:
                end += 12 * 60
    if start is None:
        for word, (s, e) in _PART_OF_DAY.items():
            if re.search(r"\b" + word + r"\b", low):
                start, end = s, e
                break
    if start is None:
        am = _AFTER_RE.search(low)
        bm = _BEFORE_RE.search(low)
        if "noon" in low and (am or bm):
            start, end = (12 * 60, 18 * 60) if am else (9 * 60, 12 * 60)
        elif am:
            start, end = _parse_time(am.group(1)), 18 * 60
        elif bm:
            start, end = 9 * 60, _parse_time(bm.group(1))
    if start is None or end is None or end <= start:
        if not days:
            return None                       # neither days nor times: not a clause
        start, end = _DEFAULT_WINDOW
    if not days:
        days = [0, 1, 2, 3, 4]
    return {"days": days, "start": int(start), "end": int(end)}


def _as_availability(availability) -> dict:
    if isinstance(availability, dict) and availability.get("windows"):
        a = dict(availability)
        a.setdefault("dur", DEFAULT_DURATION)
        a.setdefault("tz", "")
        a.setdefault("raw", "")
        return a
    if isinstance(availability, str):
        return parse_availability(availability)
    return parse_availability("")


def describe_availability(availability) -> str:
    """One human line — used in the LLM prompt so the voice stays truthful."""
    a = _as_availability(availability)
    names = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]
    parts = []
    for w in a["windows"]:
        parts.append("/".join(names[d] for d in w["days"]) +
                     f" {format_time(w['start'])}-{format_time(w['end'])}")
    tz = f" ({a['tz']})" if a.get("tz") else ""
    return "; ".join(parts) + tz


# ---------------------------------------------------------------- proposing

def propose_slots(availability, n: int = 3, tz_hint=None, today=None,
                  horizon_days: int = HORIZON_DAYS) -> list:
    """Up to n slots from the next working days that fit `availability`.

    Deterministic, pure Python, no LLM and no network. Starts at tomorrow and
    walks forward at most horizon_days, one slot per day before offering a
    second time on any day — so a proposal reads "Tue, Wed, Thu", not three
    times on one afternoon. `today` exists so tests (and any replay) can pin
    the calendar; `tz_hint` is carried on the availability for the human
    sentence only — the fixed JSON shape has no tz field.
    """
    a = _as_availability(availability)
    if tz_hint and not a.get("tz"):
        a["tz"] = str(tz_hint)
    today = today or _date.today()
    dur = int(a.get("dur") or DEFAULT_DURATION)

    by_day = []
    for offset in range(1, max(1, horizon_days) + 1):
        d = today + timedelta(days=offset)
        times = set()
        for w in a["windows"]:
            if d.weekday() in w["days"]:
                t = w["start"]
                while t + dur <= w["end"]:
                    times.add(t)
                    t += SLOT_STRIDE
        if times:
            by_day.append((d, sorted(times)))

    slots, index = [], 0
    while len(slots) < n:
        added = False
        for d, times in by_day:
            if index < len(times):
                slots.append(_make_slot(d, times[index], dur))
                added = True
                if len(slots) >= n:
                    break
        if not added:
            break
        index += 1
    return slots[:n]


def slot_fits(slot, availability, today=None) -> bool:
    """Does one proposed slot sit inside our availability, in the future?"""
    s = _slot_in(slot)
    if not s:
        return False
    a = _as_availability(availability)
    d = _parse_date(s["date"])
    today = today or _date.today()
    if d is None or d <= today or d > today + timedelta(days=365):
        return False
    start = _parse_time(s["time"])
    end = start + int(s.get("dur") or a["dur"])
    return any(d.weekday() in w["days"] and w["start"] <= start and end <= w["end"]
               for w in a["windows"])


def first_fitting_slot(slots, availability, today=None):
    for s in slots or []:
        if slot_fits(s, availability, today):
            return _slot_in(s)
    return None


# ---------------------------------------------------------------- drafting

_PARLEY_RULES = (
    "This is a scheduling email and nothing else.\n"
    "- Write 1-3 short sentences in the user's own voice.\n"
    "- Mention EVERY time listed below, exactly as written. Invent no other "
    "time, date, place, agenda, or commitment.\n"
    "- Never write a [FILL IN ...] placeholder - everything you need is below.\n"
    "- Do not output JSON, code fences, markers, or the text [parley-data]; "
    "a separate system appends that.\n"
    "- Output ONLY the body text, ending with the user's normal sign-off."
)


def _system_prompt(cfg: dict) -> str:
    base = ""
    if drafter is not None:
        try:
            base = drafter.build_system_prompt(cfg)
        except Exception:
            base = ""
    if not base:
        name = (cfg or {}).get("signature_name") or "the user"
        address = (cfg or {}).get("gmail_address") or ""
        base = f"You draft email replies on behalf of {name} <{address}>."
    return base + "\n\n" + _PARLEY_RULES


def _generate(system: str, user: str, cfg: dict, generate_fn=None) -> str:
    """Call the model, then insist the result is usable.

    Failures are raised, never swallowed: the poller hook records the error row
    (so it surfaces in the UI banner) and falls back to ordinary drafting. A
    silent fallback would turn an LLM outage into "Parley quietly stopped
    working", which is the failure nobody notices.
    """
    fn = generate_fn
    if fn is None:
        if drafter is None:
            raise RuntimeError("Parley needs mailpilot.drafter or an explicit generate_fn.")
        fn = drafter.generate
    text = (fn(system, user, cfg) or "").strip()
    text = strip_block(text)                                  # model echoed the block
    text = re.sub(r"^```[a-z]*\s*|\s*```$", "", text).strip()  # model fenced the output
    if not text:
        raise RuntimeError("Parley draft came back empty.")
    if "[FILL IN" in text:
        raise RuntimeError("Parley draft contained an unresolved [FILL IN] placeholder.")
    return text


def _slot_lines(slots) -> str:
    return "\n".join("- " + format_slot(s) for s in slots)


def _context(email_row) -> str:
    row = email_row or {}
    def g(key, default=""):
        try:
            v = row[key]
        except Exception:
            v = default
        return v or default
    return (f"You are writing to {g('from_name') or g('from_address', 'them')} "
            f"<{g('from_address')}> about \"{g('subject', '(no subject)')}\".\n"
            f"Their message:\n{str(g('body_text'))[:1200]}")


def fallback_sentence(state: str, slots, chosen=None, signature_name: str = "") -> str:
    """Deterministic text for the same email, with no model involved.

    Not used automatically — respond() raises instead of silently degrading —
    but the integrator can offer it as a one-click "draft it plainly" escape
    when the model is down, and the checks below use it as a stub.
    """
    sign = f"\n\n{signature_name}" if signature_name else ""
    if state == "accept" and chosen:
        return f"{format_slot(chosen)} works for me - I'll put it in the diary.{sign}"
    lead = "Would any of these work?" if state == "propose" else \
           "None of those quite work on my side, sorry. Would any of these?"
    return lead + "\n" + _slot_lines(slots) + sign


def start_parley(email_row, availability, cfg, generate_fn=None, today=None) -> str:
    """A fresh outbound proposal — the body for the "Parley this" button.

    Returns the FULL body: human sentences plus the labelled data block. The
    caller does not need to append anything. To recover the state to store in
    drafts.meta_json, round-trip it: ``parley.detect(None, body)`` returns
    exactly the dict that was embedded.
    """
    a = _as_availability(availability)
    slots = propose_slots(a, 3, a.get("tz"), today=today)
    if not slots:
        raise RuntimeError("No slots available - check the parley_availability setting.")
    data = {"v": PROTOCOL_VERSION, "kind": "schedule", "state": "propose",
            "round": 1, "slots": slots, "chosen": None}
    tz = f" All times {a['tz']}." if a.get("tz") else ""
    user = (
        _context(email_row) + "\n\n"
        "They want to find a time to meet. Propose these times, warmly and briefly:\n"
        + _slot_lines(slots) + tz +
        "\nSay that any of them works for you and ask them to pick one."
    )
    return render_body(_generate(_system_prompt(cfg), user, cfg, generate_fn), data)


def respond(parley_data, availability, email_row, cfg, generate_fn=None, today=None,
            max_rounds: int = MAX_ROUNDS):
    """One negotiating round.

    Returns ``(reply_body_text, new_parley_data)`` — the body is COMPLETE,
    block already appended — or ``(None, None)`` meaning "hand this to the
    human": store nothing, draft nothing, let the normal reply path or the
    human take the thread.

    Behaviour:
      their "propose"/"counter"  -> a slot of theirs fits  => we "accept" it
                                 -> nothing fits           => we "counter" at round+1
                                 -> counter would exceed max_rounds => (None, None)
      their "accept"             -> (None, None): the negotiation is over, a human
                                    should see the agreed time, not another robot round.

    The round cap is applied to COUNTERS. An accept is always allowed, because
    an accept ends the exchange rather than extending it — refusing to accept
    at the cap would throw away the outcome the whole protocol exists to reach.
    """
    data = _normalize_data(parley_data)     # idempotent: safe on detect() output too
    if not data or data["state"] not in ("propose", "counter"):
        return None, None

    a = _as_availability(availability)
    new_round = int(data["round"]) + 1
    fit = first_fitting_slot(data["slots"], a, today)

    if fit:
        new_data = {"v": PROTOCOL_VERSION, "kind": "schedule", "state": "accept",
                    "round": new_round, "slots": [fit], "chosen": fit}
        user = (_context(email_row) + "\n\n"
                "They proposed times and one of them works. Confirm exactly this one:\n"
                + "- " + format_slot(fit) +
                "\nKeep it to a sentence or two: confirm the time and say you'll put it in.")
    else:
        if new_round > max_rounds:
            return None, None
        ours = propose_slots(a, 3, a.get("tz"), today=today)
        if not ours:
            return None, None
        new_data = {"v": PROTOCOL_VERSION, "kind": "schedule", "state": "counter",
                    "round": new_round, "slots": ours, "chosen": None}
        tz = f" All times {a['tz']}." if a.get("tz") else ""
        user = (_context(email_row) + "\n\n"
                "None of the times they suggested work. Say so politely, without "
                "explaining why, and offer these instead:\n" + _slot_lines(ours) + tz)

    return render_body(_generate(_system_prompt(cfg), user, cfg, generate_fn), new_data), new_data


def handoff_reason(parley_data, availability, today=None, max_rounds: int = MAX_ROUNDS) -> str:
    """Why respond() returned (None, None) — the note to leave for the human.

    Empty string means "respond() would have produced a round", i.e. this is
    not a hand-off at all.
    """
    data = _normalize_data(parley_data)
    if not data:
        return "Parley: the scheduling data in this email could not be read - handling it normally."
    if data["state"] == "accept":
        chosen = data.get("chosen") or (data["slots"][0] if data["slots"] else None)
        when = format_slot(chosen) if chosen else "a time"
        return f"Parley: they accepted {when}. Nothing more to negotiate - confirm it yourself."
    if first_fitting_slot(data["slots"], availability, today):
        return ""
    if int(data["round"]) + 1 > max_rounds:
        return (f"Parley: {max_rounds} rounds and no overlap with your availability. "
                "Stopping here - this one needs you.")
    return ""


def is_echo(inbound, our_last) -> bool:
    """Is this 'inbound' block actually our own, quoted back at us?

    Belt to strip_quoted's braces. Compare the detected block against the block
    we last sent on the thread (drafts.meta_json): same state, same round and
    the same slot list means nobody said anything new, so there is nothing to
    answer.
    """
    a, b = _normalize_data(inbound), _normalize_data(our_last)
    if not a or not b:
        return False
    return (a["state"] == b["state"] and a["round"] == b["round"]
            and a["slots"] == b["slots"] and a["chosen"] == b["chosen"])


# ---------------------------------------------------------------- checks

if __name__ == "__main__":
    import sys

    failures = []

    def check(name, ok, detail=""):
        print(f"  {'PASS' if ok else 'FAIL'}  {name}" + (f"  {detail}" if detail else ""))
        if not ok:
            failures.append(name)

    def raises(fn) -> bool:
        try:
            fn()
        except Exception:
            return True
        return False

    def stub(system, user, cfg):
        """Offline stand-in for drafter.generate: echoes the times it was given."""
        times = re.findall(r"^- (.+)$", user, re.M)
        if "Confirm exactly this one" in user:
            return f"{times[0]} works - I'll put it in.\n\n{cfg.get('signature_name', '')}"
        lead = "Would any of these work?" if "Propose these times" in user else \
               "None of those work for me, sorry - how about:"
        return lead + "\n" + "\n".join("- " + t for t in times) + \
            f"\n\n{cfg.get('signature_name', '')}"

    CFG = {"signature_name": "Pilot", "gmail_address": "pilot@example.com", "mode": "api"}
    TODAY = _date(2026, 9, 16)          # a Wednesday
    ROW = {"from_name": "Dana", "from_address": "dana@example.com",
           "subject": "Catch up next week", "body_text": "Can we find 30 minutes?"}

    print("\nblock round-trip")
    data = {"v": 1, "kind": "schedule", "state": "propose", "round": 1,
            "slots": [{"day": "Tue", "date": "2026-09-22", "time": "14:00", "dur": 30},
                      {"day": "Wed", "date": "2026-09-23", "time": "10:00", "dur": 30}],
            "chosen": None}
    body = render_body("Would any of these work? Tue 2pm or Wed 10am.\n\nPilot", data)
    back = detect({"X-Parley": "v1"}, body)
    check("detect returns the embedded data", back is not None and back["slots"] == data["slots"], str(back and back["state"]))
    check("detect works on markers alone (no header)", detect(None, body) == back)
    check("build_block is the tail of the body", body.rstrip().endswith(CLOSE_MARKER))
    check("every block line fits in 78 columns (stays 7bit through set_content)",
          max(len(ln) for ln in build_block(data).splitlines()) <= 78,
          str(max(len(ln) for ln in build_block(data).splitlines())))
    long_dur = dict(data, slots=[dict(s, dur=120) for s in data["slots"]],
                    chosen=dict(data["slots"][0], dur=120))
    check("  ...even with a chosen slot and 3-digit durations",
          max(len(ln) for ln in build_block(long_dur).splitlines()) <= 78,
          str(max(len(ln) for ln in build_block(long_dur).splitlines())))
    check("the fallback pretty-print is still valid JSON",
          detect(None, OPEN_MARKER + "\n" + json.dumps(data, indent=2) + "\n" + CLOSE_MARKER) is not None)
    check("strip_block leaves only the human text",
          strip_block(body) == "Would any of these work? Tue 2pm or Wed 10am.\n\nPilot",
          repr(strip_block(body)))
    check("strip_block drops the label line", BLOCK_LABEL not in strip_block(body))
    check("plain email is not a parley", detect(None, "Hi, can we meet Tuesday?") is None)
    check("malformed JSON -> None",
          detect(None, "hi\n" + OPEN_MARKER + "\n{not json,,}\n" + CLOSE_MARKER) is None)
    check("header without a block -> None", detect({"X-Parley": "v1"}, "hi there") is None)
    check("wrong version -> None",
          detect(None, OPEN_MARKER + '\n{"v":2,"kind":"schedule","state":"propose"}\n' + CLOSE_MARKER) is None)
    check("unknown state -> None",
          detect(None, OPEN_MARKER + '\n{"v":1,"kind":"schedule","state":"haggle"}\n' + CLOSE_MARKER) is None)
    check("undated slot is dropped, not trusted",
          (detect(None, OPEN_MARKER + '\n{"v":1,"kind":"schedule","state":"propose","round":1,'
                        '"slots":[{"day":"Tue","time":"14:00"}]}\n' + CLOSE_MARKER) or {}).get("slots") == [])
    quoted = "Sounds good!\n\nOn Mon, Dana wrote:\n> " + body.replace("\n", "\n> ")
    check("strip_quoted defuses our own block quoted back",
          detect(None, strip_quoted(quoted)) is None)
    check("is_echo catches a verbatim bounce-back", is_echo(back, data))

    print("\nparse_availability")
    a1 = parse_availability("Tue+Thu 13:00-17:00, Fri mornings")
    check("two day-tokens joined by +", a1["windows"][0]["days"] == [1, 3], str(a1["windows"][0]))
    check("explicit 24h range", (a1["windows"][0]["start"], a1["windows"][0]["end"]) == (780, 1020))
    check("'Fri mornings'", a1["windows"][1] == {"days": [4], "start": 540, "end": 720}, str(a1["windows"][1]))
    a2 = parse_availability("weekdays 1-5pm, 45 min meetings, ET")
    check("'1-5pm' means 13:00-17:00",
          (a2["windows"][0]["start"], a2["windows"][0]["end"]) == (780, 1020), str(a2["windows"][0]))
    check("weekdays group", a2["windows"][0]["days"] == [0, 1, 2, 3, 4])
    check("duration picked up", a2["dur"] == 45, str(a2["dur"]))
    check("timezone picked up", a2["tz"] == "ET", a2["tz"])
    a3 = parse_availability("Mon and Wed after 2pm; 1 hour")
    check("'and' joins days", a3["windows"][0]["days"] == [0, 2], str(a3["windows"][0]))
    check("'after 2pm'", a3["windows"][0]["start"] == 840, str(a3["windows"][0]))
    check("'1 hour' duration", a3["dur"] == 60)
    a4 = parse_availability("Mon-Fri 9-5")
    check("bare '9-5' is 09:00-17:00",
          (a4["windows"][0]["start"], a4["windows"][0]["end"]) == (540, 1020), str(a4["windows"][0]))
    check("'Mon-Fri' is a day RANGE, not two days",
          a4["windows"][0]["days"] == [0, 1, 2, 3, 4], str(a4["windows"][0]["days"]))
    check("a wrapping day range works", parse_availability("Fri-Mon 10-11am")["windows"][0]["days"]
          == [0, 4, 5, 6], str(parse_availability("Fri-Mon 10-11am")["windows"][0]["days"]))
    check("garbage still yields a usable default",
          parse_availability("¯\\_(ツ)_/¯")["windows"] == [{"days": [0, 1, 2, 3, 4], "start": 540, "end": 1020}])
    check("empty setting yields the default window", parse_availability("")["windows"][0]["days"] == [0, 1, 2, 3, 4])

    print("\npropose_slots")
    slots = propose_slots("Tue+Thu 13:00-17:00", 3, today=TODAY)
    check("returns n slots", len(slots) == 3, str([s["date"] + " " + s["time"] for s in slots]))
    check("spreads across days before doubling up",
          len({s["date"] for s in slots}) == 3, str([s["day"] for s in slots]))
    check("only on allowed weekdays",
          all(_parse_date(s["date"]).weekday() in (1, 3) for s in slots))
    check("inside the window", all(_parse_time(s["time"]) >= 780 and
                                   _parse_time(s["time"]) + s["dur"] <= 1020 for s in slots))
    check("all in the future", all(_parse_date(s["date"]) > TODAY for s in slots))
    check("fits its own availability", all(slot_fits(s, "Tue+Thu 13:00-17:00", TODAY) for s in slots))
    check("does not fit a disjoint availability",
          not any(slot_fits(s, "Mon 09:00-10:00", TODAY) for s in slots))
    narrow = propose_slots("Fri 09:00-09:30", 3, today=TODAY)
    check("a narrow window yields fewer, not bogus, slots", len(narrow) <= 3 and narrow and
          all(s["time"] == "09:00" for s in narrow), str(len(narrow)))

    print("\nnegotiation: propose -> counter -> accept")
    A_AVAIL = "Mon+Wed 09:00-12:00"          # us
    B_AVAIL = "Wed 10:00-16:00, Fri mornings"  # them

    opener = start_parley(ROW, A_AVAIL, CFG, generate_fn=stub, today=TODAY)
    d1 = detect(None, opener)
    check("start_parley body round-trips through detect", d1 is not None and d1["state"] == "propose")
    check("opener is round 1", d1 and d1["round"] == 1)
    check("opener reads like an email", "Would any of these work?" in strip_block(opener),
          repr(strip_block(opener).splitlines()[0]))
    check("opener has no data block once stripped", OPEN_MARKER not in strip_block(opener))

    b_body, d2 = respond(d1, B_AVAIL, ROW, CFG, generate_fn=stub, today=TODAY)
    check("B counters (no overlap yet)", d2 is not None and d2["state"] == "counter", str(d2 and d2["state"]))
    check("B's counter is round 2", d2 and d2["round"] == 2)
    check("B's slots fit B", all(slot_fits(s, B_AVAIL, TODAY) for s in d2["slots"]))
    check("B's body carries B's block", detect(None, b_body)["slots"] == d2["slots"])

    a_body, d3 = respond(detect(None, b_body), A_AVAIL, ROW, CFG, generate_fn=stub, today=TODAY)
    check("A accepts", d3 is not None and d3["state"] == "accept", str(d3 and d3["state"]))
    check("A's accept is round 3", d3 and d3["round"] == 3)
    check("the chosen slot fits BOTH sides",
          slot_fits(d3["chosen"], A_AVAIL, TODAY) and slot_fits(d3["chosen"], B_AVAIL, TODAY),
          format_slot(d3["chosen"]))
    check("accept names the time in the body", format_time(_parse_time(d3["chosen"]["time"])) in a_body)
    check("no hand-off note while a round is still possible", handoff_reason(d2, A_AVAIL, TODAY) == "")

    print("\nhand-offs")
    check("inbound accept is handed to the human",
          respond(d3, B_AVAIL, ROW, CFG, generate_fn=stub, today=TODAY) == (None, None))
    check("  ...with a note", "accepted" in handoff_reason(d3, B_AVAIL, TODAY),
          handoff_reason(d3, B_AVAIL, TODAY))
    X, Y = "Mon 09:00-10:00", "Fri 15:00-16:00"          # never overlap
    p1 = detect(None, start_parley(ROW, X, CFG, generate_fn=stub, today=TODAY))
    _, p2 = respond(p1, Y, ROW, CFG, generate_fn=stub, today=TODAY)
    _, p3 = respond(p2, X, ROW, CFG, generate_fn=stub, today=TODAY)
    check("round 2 counter allowed", p2 and p2["round"] == 2)
    check("round 3 counter allowed", p3 and p3["round"] == 3)
    check("round 4 counter refused -> hand to human",
          respond(p3, Y, ROW, CFG, generate_fn=stub, today=TODAY) == (None, None))
    check("  ...with a note naming the cap", "3 rounds" in handoff_reason(p3, Y, TODAY),
          handoff_reason(p3, Y, TODAY))
    check("an empty proposal list still counters rather than crashing",
          (respond({"v": 1, "kind": "schedule", "state": "propose", "round": 1,
                    "slots": [], "chosen": None}, A_AVAIL, ROW, CFG,
                   generate_fn=stub, today=TODAY)[1] or {}).get("state") == "counter")
    check("a placeholder in the model output is refused, not sent",
          raises(lambda: respond(d1, B_AVAIL, ROW, CFG,
                                 generate_fn=lambda s, u, c: "How about [FILL IN: a time]?",
                                 today=TODAY)))
    check("an empty model response is refused, not sent",
          raises(lambda: respond(d1, B_AVAIL, ROW, CFG,
                                 generate_fn=lambda s, u, c: "   ", today=TODAY)))
    check("a model that echoes the block has it stripped, not doubled",
          respond(d1, B_AVAIL, ROW, CFG, today=TODAY,
                  generate_fn=lambda s, u, c: "How about Friday?\n" + build_block(d1)
                  )[0].count(OPEN_MARKER) == 1)

    print("\nsend-path compatibility")
    HTML_DOC_RE = re.compile(r"<!doctype html|<html[\s>]", re.I)
    check("sender.HTML_DOC_RE does not fire on a parley body", not HTML_DOC_RE.search(a_body))
    check("sender's [FILL IN guard does not fire", "[FILL IN" not in a_body)
    check("body is non-empty after strip (guard 5)", bool(strip_block(a_body).strip()))

    print("\n" + ("ALL CHECKS PASSED" if not failures else f"{len(failures)} FAILED: {failures}"))
    sys.exit(1 if failures else 0)
