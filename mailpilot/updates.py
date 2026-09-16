"""Update check + diagnostics export.

Two independent features that share one module because both are "phone home
about the app's own health, never a client's mail" surfaces:

- check_for_update() / cached_check(): a single GET against the GitHub
  releases API. Offline is the normal state for a desktop app — any failure
  (network, timeout, bad JSON, an unparseable tag) degrades silently to
  "no update", never raises, never touches the errors table. Nobody needs a
  diagnostics entry for "the laptop was asleep".
- build_diagnostics(): a plaintext bundle for Ed to attach to a bug report.
  Every secret-bearing field is reduced to presence + length via
  `config.get_secret` — the value itself is never read into this module's
  namespace, let alone printed. The log tail gets the same credential
  scrubbing `config.py` promises everywhere else (secret VALUES are never
  logged, so this is defense in depth, not the primary control).

Import shim: this module ships as `mailpilot/updates.py` (relative imports,
matching the rest of the package — see db.py's `from . import paths`). It is
also runnable standalone for the self-test at the bottom, where there is no
enclosing package at all, so the relative import is wrapped and falls back to
an absolute import with the repo root pushed onto sys.path.
"""
import json
import re
import sys
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

try:
    from . import APP_NAME, VERSION, config, db, paths
except ImportError:  # running standalone (no parent package) - see module docstring
    sys.path.insert(0, "/home/ed/mailpilot")
    from mailpilot import APP_NAME, VERSION, config, db, paths

RELEASES_URL = "https://api.github.com/repos/etatton/mailpilot/releases/latest"
REQUEST_TIMEOUT = 5

CACHE_KV_KEY = "update_check"
CACHE_HOURS = 20

_SEMVER_PART = re.compile(r"\d+")


def _parse_semver(v: str):
    """'v0.3.0' / '0.3.0' / '0.3' -> (0, 3, 0). None if it isn't numeric semver."""
    v = (v or "").strip()
    if v[:1] in ("v", "V"):
        v = v[1:]
    parts = v.split(".")[:3]
    if not parts:
        return None
    nums = []
    for p in parts:
        m = _SEMVER_PART.match(p)
        if not m:
            return None
        nums.append(int(m.group()))
    while len(nums) < 3:
        nums.append(0)
    return tuple(nums)


def check_for_update(current_version: str) -> dict:
    """One-shot GitHub releases check. Never raises; offline/blocked/malformed
    all collapse to {"newer": False} with no trace left anywhere.
    """
    try:
        req = urllib.request.Request(
            RELEASES_URL,
            headers={
                "User-Agent": f"{APP_NAME}/{current_version}",
                "Accept": "application/vnd.github+json",
            },
        )
        with urllib.request.urlopen(req, timeout=REQUEST_TIMEOUT) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        tag = str(data.get("tag_name") or "").strip()
        latest = tag[1:] if tag[:1] in ("v", "V") else tag
        url = str(data.get("html_url") or "")
        newer = bool(_parse_semver(current_version) and _parse_semver(latest)
                      and _parse_semver(latest) > _parse_semver(current_version))
        return {"latest": latest, "url": url, "newer": newer}
    except Exception:
        return {"newer": False}


def cached_check(current_version: str, db_file=None) -> dict:
    """What the server calls. At most one real HTTP hit per CACHE_HOURS,
    tracked in the kv table so it survives restarts. A corrupt/missing cache
    entry is treated the same as no cache - just check again.
    """
    try:
        raw = db.kv_get(CACHE_KV_KEY, db_file)
        if raw:
            cached = json.loads(raw)
            at = datetime.fromisoformat(cached["at"])
            if datetime.now(timezone.utc) - at < timedelta(hours=CACHE_HOURS):
                return cached["result"]
    except Exception:
        pass  # unreadable cache -> fall through to a fresh check

    result = check_for_update(current_version)

    try:
        db.kv_set(
            CACHE_KV_KEY,
            json.dumps({"at": datetime.now(timezone.utc).isoformat(), "result": result}),
            db_file,
        )
    except Exception:
        pass  # caching is best-effort; the check itself already ran either way

    return result


# --------------------------------------------------------------- diagnostics

_ANTHROPIC_KEY_RE = re.compile(r"sk-ant-\S+")
# Masks the token that follows the word "password" (any casing, any of the
# app's own log phrasings: "app_password=", "password:", "password "),
# provided that token is at least 16 chars - short enough words near
# "password" (like "not set") are left alone.
_PASSWORD_TOKEN_RE = re.compile(r"(?i)(password\w*[:=\s]+)(\S{16,})")


def _scrub(text: str) -> str:
    text = _ANTHROPIC_KEY_RE.sub("sk-ant-***REDACTED***", text)
    text = _PASSWORD_TOKEN_RE.sub(lambda m: m.group(1) + "***REDACTED***", text)
    return text


def _secret_status(name: str) -> str:
    value = config.get_secret(name)
    return f"set ({len(value)} chars)" if value else "not set"


def _config_section(cfg: dict) -> list:
    lines = ["== Config =="]
    for key in config.DEFAULTS:  # DEFAULTS holds only non-secret settings
        value = cfg.get(key, config.DEFAULTS[key])
        if isinstance(value, list):
            value = ", ".join(value) if value else "(empty)"
        lines.append(f"{key}: {value}")
    lines.append("")
    lines.append("-- secrets (presence + length only) --")
    lines.append(f"anthropic_api_key: {_secret_status('anthropic_api_key')}")
    lines.append(f"gmail_app_password (legacy): {_secret_status('gmail_app_password')}")
    address = (cfg.get("gmail_address") or "").strip()
    if address:
        name = config.gmail_secret_name(address)
        lines.append(f"gmail_app_password ({address}): {_secret_status(name)}")
    return lines


def _db_section(db_file) -> list:
    lines = ["== Database =="]
    with db.conn(db_file) as c:
        lines.append("Emails by status:")
        rows = c.execute("SELECT status, COUNT(*) n FROM emails GROUP BY status ORDER BY status").fetchall()
        for r in rows:
            lines.append(f"  {r['status']}: {r['n']}")
        if not rows:
            lines.append("  (none)")

        lines.append("Drafts by status:")
        rows = c.execute("SELECT status, COUNT(*) n FROM drafts GROUP BY status ORDER BY status").fetchall()
        for r in rows:
            lines.append(f"  {r['status']}: {r['n']}")
        if not rows:
            lines.append("  (none)")

        unacked = c.execute("SELECT COUNT(*) n FROM errors WHERE acknowledged=0").fetchone()["n"]
        lines.append(f"Unacknowledged errors: {unacked}")

        lines.append("")
        lines.append("-- newest 20 errors (traceback excluded) --")
        rows = c.execute(
            "SELECT source, message, created_at FROM errors ORDER BY id DESC LIMIT 20"
        ).fetchall()
        if not rows:
            lines.append("(none)")
        for r in rows:
            lines.append(f"[{r['created_at']}] {r['source']}: {r['message']}")
    return lines


def _log_tail_section() -> list:
    lines = ["== Log tail (last 200 lines, credentials scrubbed) =="]
    log_file = paths.log_path()
    try:
        text = log_file.read_text(encoding="utf-8", errors="replace")
    except OSError:
        lines.append("(no log file yet)")
        return lines
    tail = text.splitlines()[-200:]
    if not tail:
        lines.append("(empty)")
        return lines
    lines.extend(_scrub(line) for line in tail)
    return lines


def build_diagnostics(db_file=None) -> str:
    """Plaintext diagnostics bundle. Safe to hand to anyone helping debug a
    problem in the sense that no secret VALUE, no traceback, and nothing
    outside the last 200 log lines is in it - but it does carry real email
    subjects/addresses from the errors table and log tail, hence the header
    warning.
    """
    cfg = config.load()
    header = [
        f"{APP_NAME} diagnostics — attach this file when reporting a problem. "
        "Review before sending: it contains email subjects/addresses from your error log.",
        f"Generated: {db.now_iso()}",
        "",
        "== App ==",
        f"Version: {VERSION}",
        f"Platform: {sys.platform}",
        f"Python: {sys.version.split()[0]}",
        "",
    ]
    sections = (
        header
        + _config_section(cfg)
        + [""]
        + _db_section(db_file)
        + [""]
        + _log_tail_section()
    )
    return "\n".join(sections) + "\n"


# ------------------------------------------------------------------- self-test

if __name__ == "__main__":
    import contextlib
    import io
    import os
    import shutil
    import tempfile
    import unittest.mock as mock

    passed = 0
    failed = 0

    def check(label, cond):
        global passed, failed
        if cond:
            passed += 1
            print(f"ok   - {label}")
        else:
            failed += 1
            print(f"FAIL - {label}")

    class _FakeResponse:
        def __init__(self, payload):
            self._body = json.dumps(payload).encode("utf-8")

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def read(self):
            return self._body

    def _fake_urlopen(payload):
        def _f(req, timeout=None):
            return _FakeResponse(payload)
        return _f

    def _failing_urlopen(req, timeout=None):
        raise urllib.error.URLError("offline")

    # ---- check_for_update: newer / older / equal / garbage tag / failure ----
    with mock.patch("urllib.request.urlopen", _fake_urlopen(
            {"tag_name": "v0.3.0", "html_url": "https://example.com/v0.3.0"})):
        r = check_for_update("0.2.0")
        check("newer release detected", r == {"latest": "0.3.0", "url": "https://example.com/v0.3.0", "newer": True})

    with mock.patch("urllib.request.urlopen", _fake_urlopen(
            {"tag_name": "v0.1.0", "html_url": "https://example.com/v0.1.0"})):
        r = check_for_update("0.2.0")
        check("older release is not 'newer'", r["newer"] is False and r["latest"] == "0.1.0")

    with mock.patch("urllib.request.urlopen", _fake_urlopen(
            {"tag_name": "v0.2.0", "html_url": "https://example.com/v0.2.0"})):
        r = check_for_update("0.2.0")
        check("equal version is not 'newer'", r["newer"] is False and r["latest"] == "0.2.0")

    with mock.patch("urllib.request.urlopen", _fake_urlopen(
            {"tag_name": "banana", "html_url": "https://example.com/banana"})):
        r = check_for_update("0.2.0")
        check("garbage tag never raises and is not 'newer'", r["newer"] is False and r["latest"] == "banana")

    with mock.patch("urllib.request.urlopen", _failing_urlopen):
        r = check_for_update("0.2.0")
        check("network failure returns exactly {'newer': False}", r == {"newer": False})

    # ---- cached_check: one real hit per window ----
    tmp_dir = tempfile.mkdtemp(prefix="mailpilot-selftest-")
    try:
        tmp_db = Path(tmp_dir) / "mailpilot.db"
        db.bootstrap(tmp_db)

        calls = {"n": 0}

        def _counting_urlopen(req, timeout=None):
            calls["n"] += 1
            return _FakeResponse({"tag_name": "v9.9.9", "html_url": "https://example.com/v9.9.9"})

        with mock.patch("urllib.request.urlopen", _counting_urlopen):
            r1 = cached_check("0.2.0", db_file=tmp_db)
            r2 = cached_check("0.2.0", db_file=tmp_db)
        check("cached_check hit the network exactly once", calls["n"] == 1)
        check("cached_check returns the same cached result", r1 == r2 == {"latest": "9.9.9", "url": "https://example.com/v9.9.9", "newer": True})

        # force the cache stale and confirm it re-checks
        stale = json.dumps({
            "at": (datetime.now(timezone.utc) - timedelta(hours=CACHE_HOURS + 1)).isoformat(),
            "result": {"latest": "0.2.0", "url": "", "newer": False},
        })
        db.kv_set(CACHE_KV_KEY, stale, tmp_db)
        with mock.patch("urllib.request.urlopen", _counting_urlopen):
            cached_check("0.2.0", db_file=tmp_db)
        check("a stale cache entry triggers a fresh network hit", calls["n"] == 2)

        # ---- build_diagnostics: redaction, against an isolated app-data dir ----
        fake_data_dir = Path(tmp_dir) / "appdata"
        fake_data_dir.mkdir()
        old_xdg = os.environ.get("XDG_DATA_HOME")
        os.environ["XDG_DATA_HOME"] = str(fake_data_dir)
        try:
            FAKE_KEY = "sk-ant-selftestFAKESECRETVALUE1234567890"
            FAKE_PW = "hunter2FAKEPASSWORDTOKENxyz"

            config.update(gmail_address="probe@example.com")
            config.set_secret("anthropic_api_key", FAKE_KEY)
            config.set_secret(config.gmail_secret_name("probe@example.com"), FAKE_PW)

            with db.conn(tmp_db) as c:
                c.execute(
                    "INSERT INTO emails (message_id, from_address, subject, status, received_at)"
                    " VALUES ('m1','sender@example.com','hello','drafted',?)",
                    (db.now_iso(),),
                )
                c.execute(
                    "INSERT INTO errors (source, message, traceback, created_at) VALUES (?,?,?,?)",
                    ("drafter", "boom", "Traceback (most recent call last): boom", db.now_iso()),
                )

            log_file = paths.log_path()
            log_file.write_text(
                "\n".join([
                    "2026-09-16 00:00:00 INFO starting up",
                    f"2026-09-16 00:00:01 DEBUG api key sk-ant-selftestFAKESECRETVALUE1234567890 loaded",
                    f"2026-09-16 00:00:02 DEBUG gmail app_password={FAKE_PW} accepted",
                ]) + "\n",
                encoding="utf-8",
            )

            report = build_diagnostics(db_file=tmp_db)

            check("diagnostics mentions the app version", VERSION in report)
            check("diagnostics has the review-before-sending header", "Review before sending" in report)
            check("diagnostics reports the secret's length, not its value", "38 chars" in report or "set (" in report)
            check("full Anthropic key never appears in the report", FAKE_KEY not in report)
            check("full password token never appears in the report", FAKE_PW not in report)
            check("scrubbed marker for the Anthropic key is present", "sk-ant-***REDACTED***" in report)
            check("scrubbed marker for the password token is present", "***REDACTED***" in report)
            check("db counts section rendered", "drafted: 1" in report)
            check("unacknowledged error surfaced", "boom" in report)
            check("traceback text is excluded", "Traceback (most recent call last)" not in report)

            # No 9+ char run of either fake secret survives anywhere in the report.
            def _leaks(secret, text, window=9):
                return any(secret[i:i + window] in text for i in range(len(secret) - window + 1))

            check("no 9+ char fragment of the API key leaks", not _leaks(FAKE_KEY, report))
            check("no 9+ char fragment of the password leaks", not _leaks(FAKE_PW, report))
        finally:
            if old_xdg is None:
                os.environ.pop("XDG_DATA_HOME", None)
            else:
                os.environ["XDG_DATA_HOME"] = old_xdg
    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)

    print(f"\n{passed} passed, {failed} failed")
    sys.exit(1 if failed else 0)
