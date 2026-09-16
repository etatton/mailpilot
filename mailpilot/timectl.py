"""Time-controls primitives: quiet hours, snooze. Stdlib only.

All "local time" here means the machine's own timezone, via
`datetime.now().astimezone()` — the same pattern MailPilot's db.py already
uses for `now_iso()`. Nothing in this module touches the network or the DB;
it is pure functions over strings and datetimes so it can be unit-tested
without a running app.

HH:MM strings are 24-hour, zero-padded ("07:00", "22:00"). Empty string means
"not set" everywhere in this module.
"""
import re
from datetime import datetime, timedelta

_HHMM_RE = re.compile(r"^([01]\d|2[0-3]):([0-5]\d)$")

# option -> number of days ahead; the landing time is always 08:00 local.
_SNOOZE_OPTIONS = {"1d": 1, "3d": 3, "1w": 7}


def valid_hhmm(s: str) -> bool:
    """True iff s is a well-formed 24-hour HH:MM string. Empty string is NOT
    valid here (callers treat empty as "unset" themselves, before calling
    this) — this only validates a string someone claims is a real time."""
    return bool(_HHMM_RE.match((s or "").strip()))


def _parse_hhmm(s: str) -> tuple[int, int]:
    m = _HHMM_RE.match((s or "").strip())
    if not m:
        raise ValueError(f"invalid HH:MM: {s!r}")
    return int(m.group(1)), int(m.group(2))


def in_quiet_hours(quiet_start: str, quiet_end: str, now=None) -> bool:
    """Is `now` (default: local now) inside the [quiet_start, quiet_end)
    window? Either side empty, or either side malformed, means quiet hours
    are off -> False. Handles windows that cross midnight (e.g. 22:00 ->
    07:00): start-of-day and end-of-day are stitched as one continuous
    window that wraps through 00:00.

    A window where start == end is treated as off (not "24 hours of quiet"),
    since that's almost certainly a misconfiguration and "off" is the safe
    failure mode for a feature that suppresses notifications.
    """
    quiet_start = (quiet_start or "").strip()
    quiet_end = (quiet_end or "").strip()
    if not quiet_start or not quiet_end:
        return False
    try:
        sh, sm = _parse_hhmm(quiet_start)
        eh, em = _parse_hhmm(quiet_end)
    except ValueError:
        return False

    now = now or datetime.now().astimezone()
    cur = now.hour * 60 + now.minute
    start = sh * 60 + sm
    end = eh * 60 + em

    if start == end:
        return False
    if start < end:
        return start <= cur < end
    return cur >= start or cur < end  # crosses midnight


def snooze_until(option: str, now=None) -> str:
    """ISO-8601 TEXT (with UTC offset) for 08:00 LOCAL on the target day.

    option is one of "1d" / "3d" / "1w", counted in calendar days from
    `now`'s date (not "now + N*24h" — a snooze started at 23:50 on "1d"
    still lands on tomorrow's 08:00, not the day after). Raises ValueError
    on an unknown option so the caller (the /snooze endpoint) can turn that
    into a 400 instead of silently defaulting to something.

    The offset attached is resolved for the TARGET date, not today's — built
    by constructing a naive datetime for that date and calling
    .astimezone() on IT (not reusing `now`'s tzinfo), so a snooze that spans
    a DST transition still reads 08:00 on the clock on the target day rather
    than 08:00 plus/minus the DST delta.
    """
    days = _SNOOZE_OPTIONS.get(option)
    if days is None:
        raise ValueError(f"unknown snooze option: {option!r} (use 1d, 3d, or 1w)")
    now = now or datetime.now().astimezone()
    target_date = (now + timedelta(days=days)).date()
    naive_target = datetime(target_date.year, target_date.month, target_date.day, 8, 0, 0)
    aware_target = naive_target.astimezone()
    return aware_target.isoformat(timespec="seconds")


def is_snoozed(snoozed_until_iso: str, now=None) -> bool:
    """True iff snoozed_until_iso parses and is still in the future.

    Empty string, None, or anything datetime.fromisoformat can't parse is
    "not snoozed" (fail open — a corrupt/garbage value must never hide a
    draft forever). A value with no UTC offset is treated as local time,
    resolved via the same naive-.astimezone() trick as snooze_until so it's
    compared on equal footing with `now`.
    """
    s = (snoozed_until_iso or "").strip()
    if not s:
        return False
    try:
        dt = datetime.fromisoformat(s)
    except ValueError:
        return False
    if dt.tzinfo is None:
        dt = dt.astimezone()
    now = now or datetime.now().astimezone()
    return dt > now


if __name__ == "__main__":
    import sys

    failures = []

    def check(label, got, want):
        ok = got == want
        print(f"{'PASS' if ok else 'FAIL':4} {label}: got={got!r} want={want!r}")
        if not ok:
            failures.append(label)

    def mk(h, m):
        # Fixed-offset aware datetime; in_quiet_hours/is_snoozed only look at
        # wall-clock fields so the actual offset chosen doesn't matter.
        from datetime import timezone
        return datetime(2026, 9, 16, h, m, tzinfo=timezone(timedelta(hours=-4)))

    print("--- in_quiet_hours: off / malformed ---")
    check("both empty = off", in_quiet_hours("", "", now=mk(23, 0)), False)
    check("start empty = off", in_quiet_hours("", "07:00", now=mk(23, 0)), False)
    check("end empty = off", in_quiet_hours("22:00", "", now=mk(23, 0)), False)
    check("garbage start = off", in_quiet_hours("not-a-time", "07:00", now=mk(23, 0)), False)
    check("garbage end = off", in_quiet_hours("22:00", "25:99", now=mk(23, 0)), False)
    check("start == end = off", in_quiet_hours("09:00", "09:00", now=mk(9, 0)), False)

    print("--- in_quiet_hours: normal (non-wrapping) window 13:00-18:00 ---")
    check("before window", in_quiet_hours("13:00", "18:00", now=mk(12, 59)), False)
    check("at start (inclusive)", in_quiet_hours("13:00", "18:00", now=mk(13, 0)), True)
    check("mid window", in_quiet_hours("13:00", "18:00", now=mk(15, 30)), True)
    check("at end (exclusive)", in_quiet_hours("13:00", "18:00", now=mk(18, 0)), False)
    check("after window", in_quiet_hours("13:00", "18:00", now=mk(19, 0)), False)

    print("--- in_quiet_hours: crossing midnight 22:00-07:00 ---")
    check("evening inside", in_quiet_hours("22:00", "07:00", now=mk(23, 0)), True)
    check("at start (inclusive)", in_quiet_hours("22:00", "07:00", now=mk(22, 0)), True)
    check("just after midnight", in_quiet_hours("22:00", "07:00", now=mk(0, 30)), True)
    check("at end (exclusive)", in_quiet_hours("22:00", "07:00", now=mk(7, 0)), False)
    check("midday outside", in_quiet_hours("22:00", "07:00", now=mk(12, 0)), False)
    check("just before start", in_quiet_hours("22:00", "07:00", now=mk(21, 59)), False)

    print("--- snooze_until ---")
    base = mk(23, 50)  # near end of day, to catch off-by-one-day bugs
    for opt, days in (("1d", 1), ("3d", 3), ("1w", 7)):
        result = snooze_until(opt, now=base)
        parsed = datetime.fromisoformat(result)
        want_date = (base + timedelta(days=days)).date()
        ok = (parsed.date() == want_date and parsed.hour == 8
              and parsed.minute == 0 and parsed.second == 0 and parsed.tzinfo is not None)
        print(f"{'PASS' if ok else 'FAIL':4} snooze_until({opt!r}): {result!r}"
              f" (expect date={want_date}, 08:00:00 local, tz-aware)")
        if not ok:
            failures.append(f"snooze_until({opt!r})")
    try:
        snooze_until("2d")
        print("FAIL snooze_until('2d') should have raised ValueError")
        failures.append("snooze_until bad option")
    except ValueError:
        print("PASS snooze_until('2d') raises ValueError")

    print("--- is_snoozed ---")
    now = mk(12, 0)
    future = (now + timedelta(days=1)).isoformat(timespec="seconds")
    past = (now - timedelta(days=1)).isoformat(timespec="seconds")
    exactly_now = now.isoformat(timespec="seconds")
    check("empty string", is_snoozed("", now=now), False)
    check("None-ish (empty after strip)", is_snoozed("   ", now=now), False)
    check("garbage", is_snoozed("banana", now=now), False)
    check("garbage-ish partial iso", is_snoozed("2026-13-40", now=now), False)
    check("future timestamp", is_snoozed(future, now=now), True)
    check("past timestamp", is_snoozed(past, now=now), False)
    check("exactly now (not strictly future)", is_snoozed(exactly_now, now=now), False)
    naive_future = (now + timedelta(days=1)).replace(tzinfo=None).isoformat(timespec="seconds")
    check("naive future (no offset) treated as local", is_snoozed(naive_future, now=now), True)

    print()
    if failures:
        print(f"{len(failures)} FAILURE(S): {failures}")
        sys.exit(1)
    print("All checks passed.")
