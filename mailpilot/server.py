"""FastAPI app: setup wizard + review queue UI + JSON API.

Binds 127.0.0.1 only. Mutating endpoints require the X-MailPilot header, which
cross-site forms can't set - a cheap CSRF stop for a localhost app.
"""
import sys
import threading
import traceback
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request
import json
import uuid

from fastapi.responses import FileResponse, JSONResponse, PlainTextResponse

from . import (VERSION, autostart, config, db, drafter, negotiation, parley,
               poller, radar, rehearsal, sender, timectl, updates)

poll_loop: poller.PollLoop | None = None


def web_dir() -> Path:
    if getattr(sys, "frozen", False):
        return Path(sys._MEIPASS) / "web"  # type: ignore[attr-defined]
    return Path(__file__).resolve().parent.parent / "web"


@asynccontextmanager
async def lifespan(app: FastAPI):
    global poll_loop
    db.bootstrap()
    config.migrate_legacy_gmail_secret()
    poll_loop = poller.PollLoop()
    poll_loop.start()
    yield
    poll_loop.stop()


app = FastAPI(lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)


def _require_header(request: Request):
    if request.headers.get("X-MailPilot") != "1":
        raise HTTPException(status_code=403, detail="missing app header")


@app.get("/api/ping")
def ping():
    return {"app": "mailpilot", "version": VERSION}


@app.get("/")
def index():
    page = "app.html" if config.load().get("configured") else "wizard.html"
    return FileResponse(web_dir() / page)


def _account_row(address: str = ""):
    """The accounts row for an address, or the primary inbox when none given."""
    wanted = (address or "").strip().lower()
    with db.conn() as c:
        if wanted:
            row = c.execute("SELECT * FROM accounts WHERE lower(address)=?", (wanted,)).fetchone()
        else:
            row = c.execute("SELECT * FROM accounts ORDER BY id LIMIT 1").fetchone()
    if row is None:
        raise HTTPException(400, "No such inbox.")
    return row


def _account_creds(address: str = "") -> tuple[str, str]:
    row = _account_row(address)
    pw = poller.account_password(row)
    if not pw:
        raise HTTPException(400, f"No app password stored for {row['address']}.")
    return row["address"], pw


def _feature(name: str) -> dict:
    cfg = config.load()
    if not cfg.get(name):
        raise HTTPException(404, "That feature is switched off in Settings > Labs.")
    return cfg


def _accounts_list() -> list[dict]:
    """Inboxes for the UI. Never carries a password - only id/address/label."""
    try:
        with db.conn() as c:
            rows = c.execute(
                "SELECT id, address, label FROM accounts ORDER BY id"
            ).fetchall()
        return [dict(r) for r in rows]
    except Exception:
        return []


@app.get("/api/state")
def state():
    cfg = config.load()
    return {
        "version": VERSION,
        "accounts": _accounts_list(),
        "configured": cfg["configured"],
        "mode": cfg["mode"],
        "model": cfg["model"],
        "live_send": cfg["live_send"],
        "gmail_address": cfg["gmail_address"],
        "signature_name": cfg["signature_name"],
        "tone_notes": cfg["tone_notes"],
        "ignore_senders": cfg["ignore_senders"],
        "only_senders": cfg["only_senders"],
        "poll_interval": cfg["poll_interval"],
        "autostart_installed": cfg["autostart_installed"],
        "notify_enabled": cfg["notify_enabled"],
        "notify_cooldown_minutes": cfg["notify_cooldown_minutes"],
        "followup_days": cfg["followup_days"],
        "quiet_start": cfg["quiet_start"],
        "quiet_end": cfg["quiet_end"],
        "vacation_mode": cfg["vacation_mode"],
        "feature_negotiation": cfg["feature_negotiation"],
        "feature_rehearsal": cfg["feature_rehearsal"],
        "feature_radar": cfg["feature_radar"],
        "feature_parley": cfg["feature_parley"],
        "negotiation_autodetect": cfg["negotiation_autodetect"],
        "parley_availability": cfg["parley_availability"],
        "update": updates.cached_check(VERSION),
        "keyring_ok": cfg["keyring_ok"],
        "platform": sys.platform,
        "claude_cli": drafter.find_claude_cli(),
        "last_poll": getattr(poll_loop, "last_run", ""),
        "last_poll_result": getattr(poll_loop, "last_result", {}),
    }


# ------------------------------------------------------------- setup wizard

@app.post("/api/setup/validate-gmail")
async def validate_gmail(request: Request):
    _require_header(request)
    data = await request.json()
    ok, msg = poller.validate_gmail(
        (data.get("email") or "").strip(),
        (data.get("app_password") or "").replace(" ", ""),
    )
    return {"ok": ok, "message": msg}


@app.post("/api/setup/validate-key")
async def validate_key(request: Request):
    _require_header(request)
    data = await request.json()
    ok, msg = drafter.validate_api_key((data.get("key") or "").strip())
    return {"ok": ok, "message": msg}


@app.get("/api/setup/detect-claude")
def detect_claude():
    # A manually-located path wins; fall back to auto-detection.
    stored = (config.load().get("claude_cli_path") or "").strip()
    if stored and (stored.startswith("wsl:") or Path(stored).exists()):
        return {"found": True, "path": stored, "manual": True}
    path = drafter.find_claude_cli()
    return {"found": bool(path), "path": path, "manual": False}


@app.post("/api/setup/claude-path")
async def set_claude_path(request: Request):
    """Manual escape hatch: the user points MailPilot at their claude program
    (plain path, or wsl:<path> for a WSL install). Verified by running
    --version before anything is saved."""
    _require_header(request)
    data = await request.json()
    path = (data.get("path") or "").strip().strip('"')
    ok, msg = drafter.validate_claude_cli(path)
    if ok:
        config.update(claude_cli_path=path)
    return {"ok": ok, "message": msg, "path": path if ok else ""}


@app.post("/api/setup/save")
async def setup_save(request: Request):
    _require_header(request)
    data = await request.json()
    mode = data.get("mode", "api")
    changes = {
        "configured": True,
        "mode": mode,
        "model": data.get("model") or "claude-opus-5",
        "gmail_address": (data.get("gmail_address") or "").strip(),
        "signature_name": (data.get("signature_name") or "").strip(),
        "tone_notes": (data.get("tone_notes") or "").strip(),
        "ignore_senders": _split(data.get("ignore_senders")),
        "only_senders": _split(data.get("only_senders")),
        "poll_interval": max(60, int(data.get("poll_interval") or 300)),
        "live_send": False,  # every new setup starts in test mode
        # A manually-located CLI path must survive setup; only fill by
        # auto-detection when nothing is stored yet.
        "claude_cli_path": (config.load().get("claude_cli_path")
                            or (drafter.find_claude_cli() if mode == "cli" else "")),
    }
    address = changes["gmail_address"]
    app_password = (data.get("app_password") or "").replace(" ", "")
    if app_password:
        # Legacy name kept so a downgrade still finds it; the per-account name
        # below is what the poller and sender actually read.
        config.set_secret("gmail_app_password", app_password)
    if address:
        with db.conn() as c:
            c.execute(
                "INSERT INTO accounts (address, label, created_at) VALUES (?,?,?)"
                " ON CONFLICT(address) DO NOTHING",
                (address.lower(), "Primary", db.now_iso()),
            )
        if app_password:
            config.set_secret(config.gmail_secret_name(address), app_password)
    if mode == "api" and data.get("api_key"):
        config.set_secret("anthropic_api_key", data["api_key"].strip())
    sample = (data.get("writing_sample") or "").strip()
    if len(sample) >= 40:
        with db.conn() as c:
            c.execute("INSERT INTO voice_samples (body, created_at) VALUES (?,?)",
                      (sample[:20000], db.now_iso()))
    autostart_msg = ""
    if data.get("autostart"):
        ok, autostart_msg = autostart.install()
        changes["autostart_installed"] = ok
    config.update(**changes)
    if poll_loop:
        poll_loop.wake.set()
    return {"ok": True, "autostart_message": autostart_msg}


def _split(v) -> list[str]:
    if isinstance(v, list):
        return [s.strip() for s in v if s.strip()]
    return [s.strip() for s in (v or "").split(",") if s.strip()]


# ------------------------------------------------------------- inboxes (accounts)

@app.get("/api/accounts")
def accounts():
    return {"accounts": _accounts_list()}


@app.post("/api/accounts")
async def add_account(request: Request):
    """Add an inbox. The credential is proved against Gmail BEFORE anything is
    stored, so a typo can never leave a half-configured account that fails
    silently on every later cycle."""
    _require_header(request)
    data = await request.json()
    address = (data.get("address") or "").strip().lower()
    app_password = (data.get("app_password") or "").replace(" ", "")
    label = (data.get("label") or "").strip()
    if not address or "@" not in address or " " in address:
        return {"ok": False, "message": "Enter the full Gmail address for this inbox."}
    if not app_password:
        return {"ok": False, "message": "This inbox needs its own 16-character app password."}
    with db.conn() as c:
        dup = c.execute("SELECT id FROM accounts WHERE address=?", (address,)).fetchone()
    if dup:
        return {"ok": False, "message": f"{address} is already connected."}

    ok, msg = poller.validate_gmail(address, app_password)
    if not ok:
        return {"ok": False, "message": msg}

    with db.conn() as c:
        cur = c.execute(
            "INSERT INTO accounts (address, label, created_at) VALUES (?,?,?)",
            (address, label, db.now_iso()),
        )
        account_id = cur.lastrowid
    config.set_secret(config.gmail_secret_name(address), app_password)
    if poll_loop:
        poll_loop.wake.set()
    return {"ok": True, "id": account_id, "message": f"{address} connected."}


@app.delete("/api/accounts/{account_id}")
def delete_account(account_id: int, request: Request):
    """Removes the inbox row only. Its stored app password is left alone (and
    never echoed) - re-adding the address re-validates and overwrites it."""
    _require_header(request)
    if account_id == 1:
        return JSONResponse(
            {"ok": False, "message": "The primary inbox can't be removed - it's the "
                                     "address MailPilot sends its own notifications to. "
                                     "Change it in Settings instead."},
            status_code=400,
        )
    with db.conn() as c:
        row = c.execute("SELECT address FROM accounts WHERE id=?", (account_id,)).fetchone()
        if row is None:
            raise HTTPException(404, "no such inbox")
        c.execute("DELETE FROM accounts WHERE id=?", (account_id,))
    return {"ok": True, "message": f"{row['address']} removed."}


# ------------------------------------------------------------- queue + history

def _drafts_where(clause: str, params=(), order: str = "d.updated_at DESC"):
    with db.conn() as c:
        rows = c.execute(
            "SELECT d.id, d.body, d.status, d.kind, d.block_reason, d.created_at,"
            " d.updated_at, d.sent_at, d.snoozed_until, d.meta_json,"
            " e.from_address, e.from_name, e.subject,"
            " e.body_text, e.received_at, e.vip, e.account_id, a.address AS account"
            f" FROM drafts d JOIN emails e ON e.id = d.email_id"
            " LEFT JOIN accounts a ON a.id = e.account_id"
            f" WHERE {clause}"
            f" ORDER BY {order} LIMIT 200",
            params,
        ).fetchall()
    return [dict(r) for r in rows]


@app.get("/api/queue")
def queue():
    all_queued = _drafts_where("d.status='queued'", order="e.vip DESC, d.updated_at DESC")
    visible, snoozed_count = [], 0
    for d in all_queued:
        if timectl.is_snoozed(d.get("snoozed_until") or ""):
            snoozed_count += 1
        else:
            visible.append(d)
    # A draft returning from snooze surfaces at the top (stable sort keeps the
    # vip/updated_at order within each group).
    visible.sort(key=lambda d: 0 if d.get("snoozed_until") else 1)
    return {"drafts": visible, "snoozed_count": snoozed_count}


@app.get("/api/history")
def history():
    return {"drafts": _drafts_where("d.status IN ('sent','simulated','discarded','blocked')")}


@app.get("/api/skipped")
def skipped():
    with db.conn() as c:
        rows = c.execute(
            "SELECT e.id, e.from_address, e.from_name, e.subject, e.received_at,"
            " e.skip_reason, e.account_id, a.address AS account"
            " FROM emails e LEFT JOIN accounts a ON a.id = e.account_id"
            " WHERE e.status='skipped' AND e.skip_reason != 'radar_seed'"
            " ORDER BY e.id DESC LIMIT 200"
        ).fetchall()
    return {"emails": [dict(r) for r in rows]}


@app.get("/api/errors")
def errors():
    with db.conn() as c:
        rows = c.execute(
            "SELECT id, source, message, created_at FROM errors"
            " WHERE acknowledged=0 ORDER BY id DESC LIMIT 50"
        ).fetchall()
    return {"errors": [dict(r) for r in rows]}


@app.get("/api/diagnostics")
def diagnostics():
    report = updates.build_diagnostics()
    return PlainTextResponse(
        report,
        headers={"Content-Disposition": 'attachment; filename="mailpilot-diagnostics.txt"'},
    )


@app.post("/api/errors/{error_id}/ack")
def ack_error(error_id: int, request: Request):
    _require_header(request)
    with db.conn() as c:
        c.execute("UPDATE errors SET acknowledged=1 WHERE id<=?", (error_id,))
    return {"ok": True}


# ------------------------------------------------------------- draft actions

@app.patch("/api/drafts/{draft_id}")
async def edit_draft(draft_id: int, request: Request):
    _require_header(request)
    data = await request.json()
    with db.conn() as c:
        c.execute(
            "UPDATE drafts SET body=?, updated_at=? WHERE id=? AND status='queued'",
            (data.get("body", ""), db.now_iso(), draft_id),
        )
    return {"ok": True}


@app.post("/api/drafts/{draft_id}/approve")
async def approve_draft(draft_id: int, request: Request):
    _require_header(request)
    data = await request.json() if int(request.headers.get("content-length") or 0) else {}
    with db.conn() as c:
        row = c.execute("SELECT status FROM drafts WHERE id=?", (draft_id,)).fetchone()
        if row is None:
            raise HTTPException(404, "no such draft")
        if row["status"] != "queued":
            raise HTTPException(409, f"draft is {row['status']}, not queued")
        if data.get("body") is not None:
            c.execute("UPDATE drafts SET body=? WHERE id=?", (data["body"], draft_id))
        c.execute(
            "UPDATE drafts SET status='approved', updated_at=? WHERE id=?",
            (db.now_iso(), draft_id),
        )
    return sender.send_reply(draft_id)


@app.post("/api/drafts/{draft_id}/regenerate")
async def regenerate_draft(draft_id: int, request: Request):
    _require_header(request)
    data = await request.json()
    with db.conn() as c:
        draft = c.execute("SELECT * FROM drafts WHERE id=?", (draft_id,)).fetchone()
        if draft is None:
            raise HTTPException(404, "no such draft")
        em = c.execute("SELECT * FROM emails WHERE id=?", (draft["email_id"],)).fetchone()
    try:
        body = drafter.draft_reply(
            em, guidance=(data.get("guidance") or "").strip(), previous_draft=draft["body"]
        )
    except Exception as e:
        db.record_error("drafter", str(e), traceback.format_exc())
        return JSONResponse({"ok": False, "message": str(e)[:300]}, status_code=502)
    with db.conn() as c:
        c.execute(
            "UPDATE drafts SET body=?, status='queued', updated_at=? WHERE id=?",
            (body, db.now_iso(), draft_id),
        )
    return {"ok": True, "body": body}


@app.post("/api/drafts/{draft_id}/discard")
def discard_draft(draft_id: int, request: Request):
    _require_header(request)
    with db.conn() as c:
        c.execute(
            "UPDATE drafts SET status='discarded', updated_at=? WHERE id=? AND status='queued'",
            (db.now_iso(), draft_id),
        )
    return {"ok": True}


@app.post("/api/drafts/{draft_id}/parley")
async def start_parley_draft(draft_id: int, request: Request):
    _require_header(request)
    cfg = config.load()
    if not cfg.get("feature_parley"):
        raise HTTPException(409, "Parley is off - turn it on in Settings > Labs.")
    if not (cfg.get("parley_availability") or "").strip():
        raise HTTPException(400, "Set your availability in Settings > Labs first.")
    with db.conn() as c:
        draft = c.execute("SELECT * FROM drafts WHERE id=?", (draft_id,)).fetchone()
        if draft is None:
            raise HTTPException(404, "no such draft")
        if draft["status"] != "queued":
            raise HTTPException(409, f"draft is {draft['status']}, not queued")
        em = c.execute("SELECT * FROM emails WHERE id=?", (draft["email_id"],)).fetchone()
    try:
        body = parley.start_parley(em, cfg["parley_availability"], cfg)
    except Exception as e:
        db.record_error("parley", str(e), traceback.format_exc())
        return JSONResponse({"ok": False, "message": str(e)[:300]}, status_code=502)
    # Round-trip the state out of the exact bytes that will be sent - one
    # definition of truth.
    data = parley.detect(None, body)
    with db.conn() as c:
        c.execute(
            "UPDATE drafts SET body=?, kind='parley', meta_json=?, status='queued',"
            " updated_at=? WHERE id=? AND status='queued'",
            (body, json.dumps(data), db.now_iso(), draft_id),
        )
    return {"ok": True, "body": body, "parley": data}


@app.post("/api/drafts/{draft_id}/negotiate")
def negotiate_draft(draft_id: int, request: Request):
    _require_header(request)
    cfg = config.load()
    if not cfg.get("feature_negotiation"):
        raise HTTPException(403, "Negotiation Copilot is off. Turn it on in Settings > Labs.")
    with db.conn() as c:
        draft = c.execute("SELECT * FROM drafts WHERE id=?", (draft_id,)).fetchone()
        if draft is None:
            raise HTTPException(404, "no such draft")
        em = c.execute("SELECT * FROM emails WHERE id=?", (draft["email_id"],)).fetchone()
    try:
        result = negotiation.draft_negotiation(em, cfg)
    except Exception as e:
        db.record_error("negotiation", str(e), traceback.format_exc())
        return JSONResponse({"ok": False, "message": str(e)[:300]}, status_code=502)
    stances = result["stances"]
    with db.conn() as c:
        c.execute(
            "UPDATE drafts SET body=?, meta_json=?, status='queued', updated_at=? WHERE id=?",
            (stances[1]["body"], json.dumps(result), db.now_iso(), draft_id),
        )
    return {"ok": True, "stances": stances}


@app.post("/api/drafts/{draft_id}/snooze")
async def snooze_draft(draft_id: int, request: Request):
    _require_header(request)
    data = await request.json()
    option = (data.get("option") or "").strip()
    try:
        until = timectl.snooze_until(option)
    except ValueError:
        raise HTTPException(400, "Unknown snooze option (use 1d, 3d, or 1w).")
    with db.conn() as c:
        row = c.execute("SELECT status FROM drafts WHERE id=?", (draft_id,)).fetchone()
        if row is None:
            raise HTTPException(404, "no such draft")
        if row["status"] != "queued":
            raise HTTPException(409, f"draft is {row['status']}, not queued")
        c.execute(
            "UPDATE drafts SET snoozed_until=?, notified=1, updated_at=? WHERE id=?",
            (until, db.now_iso(), draft_id),
        )
    return {"ok": True, "snoozed_until": until}


# ------------------------------------------------------------- rehearsal

@app.get("/api/rehearsal/candidates")
def rehearsal_candidates(request: Request, account: str = "", limit: int = 25):
    _feature("feature_rehearsal")
    addr, pw = _account_creds(account)
    try:
        return {"account": addr, "candidates": rehearsal.list_sent_candidates(addr, pw, limit)}
    except Exception as e:
        db.record_error("rehearsal", str(e), traceback.format_exc())
        return JSONResponse({"ok": False, "message": str(e)[:300]}, status_code=502)


@app.post("/api/rehearsal/run")
async def rehearsal_run(request: Request):
    _require_header(request)
    cfg = _feature("feature_rehearsal")
    data = await request.json()
    addr, pw = _account_creds(data.get("account") or "")
    picks = []
    for item in (data.get("seqs") or [])[:rehearsal.MAX_REHEARSALS_PER_RUN]:
        if isinstance(item, dict):
            picks.append((int(item.get("seq") or 0), item.get("message_id") or ""))
        else:
            picks.append((int(item), ""))
    pairs, missing = [], 0
    for seq, mid in picks:
        try:
            pair = rehearsal.fetch_pair(addr, pw, seq, expect_message_id=mid)
        except Exception as e:
            db.record_error("rehearsal", f"fetch_pair({seq}): {e}", traceback.format_exc())
            pair = None
        if pair:
            pairs.append(pair)
        else:
            missing += 1
    result = rehearsal.run_rehearsal(pairs, cfg)
    result["missing_original"] = missing
    return result


@app.get("/api/rehearsal/results")
def rehearsal_results():
    _feature("feature_rehearsal")
    return {"rehearsals": rehearsal.list_rehearsals()}


@app.post("/api/rehearsal/{rehearsal_id}/save-voice-sample")
def rehearsal_save_voice(rehearsal_id: int, request: Request):
    _require_header(request)
    _feature("feature_rehearsal")
    if not rehearsal.save_as_voice_sample(rehearsal_id):
        raise HTTPException(400, "That reply is too short to be a useful voice sample.")
    return {"ok": True}


# ------------------------------------------------------------- radar

_radar_threads: dict[str, threading.Thread] = {}


def _radar_backfill_thread(addr: str, pw: str, force: bool):
    try:
        radar.backfill(addr, pw, progress_cb=radar.kv_progress_cb(addr), force=force)
    except Exception as e:
        db.record_error("radar", f"Backfill thread died: {e}", traceback.format_exc())


@app.get("/api/radar")
def radar_list():
    _feature("feature_radar")
    cfg = config.load()
    addr = (cfg.get("gmail_address") or "").lower()
    return {
        "drifted": radar.compute_drift(),
        "backfilled": bool(db.kv_get(radar.BACKFILL_DONE_KEY + addr)),
        "progress": db.kv_get(radar.BACKFILL_PROGRESS_KEY + addr),
    }


@app.get("/api/radar/backfill/status")
def radar_backfill_status(account: str = ""):
    _feature("feature_radar")
    addr = (account or config.load().get("gmail_address") or "").lower()
    t = _radar_threads.get(addr)
    return {"running": bool(t and t.is_alive()),
            "progress": db.kv_get(radar.BACKFILL_PROGRESS_KEY + addr),
            "done": db.kv_get(radar.BACKFILL_DONE_KEY + addr)}


@app.post("/api/radar/backfill")
async def radar_backfill(request: Request):
    _require_header(request)
    _feature("feature_radar")
    data = await request.json()
    addr, pw = _account_creds(data.get("account_address") or "")
    t = _radar_threads.get(addr)
    if t and t.is_alive():
        return {"ok": True, "status": "already_running"}
    t = threading.Thread(target=_radar_backfill_thread,
                         args=(addr, pw, bool(data.get("force"))),
                         daemon=True, name=f"radar-backfill-{addr}")
    _radar_threads[addr] = t
    t.start()
    return {"ok": True, "status": "started"}


@app.post("/api/radar/dismiss")
async def radar_dismiss(request: Request):
    _require_header(request)
    _feature("feature_radar")
    data = await request.json()
    until = radar.dismiss((data.get("address") or ""), int(data.get("days") or 90))
    if not until:
        raise HTTPException(400, "No address given.")
    return {"ok": True, "until": until}


@app.post("/api/radar/reconnect")
async def radar_reconnect(request: Request):
    _require_header(request)
    cfg = _feature("feature_radar")
    data = await request.json()
    address = (data.get("address") or "").strip().lower()
    if not address or "@" not in address:
        raise HTTPException(400, "No address given.")
    hit = next((d for d in radar.compute_drift() if d["address"] == address), {})
    user = radar.build_reconnect_prompt(
        address, data.get("name_hint") or hit.get("name_hint", ""),
        int(data.get("weeks_since") or hit.get("weeks_since") or 8),
        hit.get("baseline", ""),
    )
    try:
        body = drafter.generate(drafter.build_system_prompt(cfg), user, cfg)
    except Exception as e:
        db.record_error("radar", str(e), traceback.format_exc())
        return JSONResponse({"ok": False, "message": str(e)[:300]}, status_code=502)
    now = db.now_iso()
    # A reconnection has no inbound email, so seed a minimal, clearly-marked row:
    # the queue, guards and history all key on one, and Guard 2 needs a real
    # accounts row (its address is who the mail goes out AS).
    account = _account_row(data.get("account_address") or "")
    message_id = f"<radar-seed-{uuid.uuid4().hex}@mailpilot>"
    with db.conn() as c:
        cur = c.execute(
            "INSERT INTO emails (message_id, from_address, from_name, subject, body_text,"
            " received_at, processed_at, status, skip_reason, account_id)"
            " VALUES (?,?,?,?,?,?,?, 'skipped', 'radar_seed', ?)",
            (message_id, address, hit.get("name_hint", ""), "Catching up",
             f"[Relationship Radar] No exchange since {hit.get('last_contact_iso', 'a while ago')}"
             f" (baseline: {hit.get('baseline', 'regular contact')}).",
             now, now, account["id"]),
        )
        email_id = cur.lastrowid
        c.execute(
            "INSERT INTO drafts (email_id, body, status, kind, created_at, updated_at)"
            " VALUES (?,?, 'queued', 'reconnect', ?, ?)",
            (email_id, body, now, now),
        )
    return {"ok": True, "email_id": email_id}


# ------------------------------------------------------------- contacts

@app.get("/api/contacts")
def contacts():
    with db.conn() as c:
        rows = c.execute("SELECT * FROM contacts ORDER BY address").fetchall()
    return {"contacts": [dict(r) for r in rows]}


@app.post("/api/contacts")
async def upsert_contact(request: Request):
    _require_header(request)
    data = await request.json()
    address = (data.get("address") or "").lower().strip()
    if not address or " " in address:
        raise HTTPException(400, "Enter an email address or a bare domain.")
    rule = data.get("rule") or "normal"
    if rule not in ("normal", "vip", "always_draft", "auto_skip"):
        raise HTTPException(400, "Unknown rule.")
    with db.conn() as c:
        c.execute(
            "INSERT INTO contacts (address, name, notes, rule, created_at)"
            " VALUES (?,?,?,?,?)"
            " ON CONFLICT(address) DO UPDATE SET name=excluded.name,"
            " notes=excluded.notes, rule=excluded.rule",
            (address, (data.get("name") or "").strip(),
             (data.get("notes") or "").strip(), rule, db.now_iso()),
        )
    return {"ok": True}


@app.delete("/api/contacts/{contact_id}")
def delete_contact(contact_id: int, request: Request):
    _require_header(request)
    with db.conn() as c:
        c.execute("DELETE FROM contacts WHERE id=?", (contact_id,))
    return {"ok": True}


# ------------------------------------------------------------- voice

@app.get("/api/voice")
def voice():
    with db.conn() as c:
        rows = c.execute(
            "SELECT id, body, created_at FROM voice_samples ORDER BY id DESC"
        ).fetchall()
    return {"samples": [dict(r) for r in rows], "profile": config.load().get("voice_profile", "")}


@app.post("/api/voice/samples")
async def add_sample(request: Request):
    _require_header(request)
    data = await request.json()
    body = (data.get("body") or "").strip()
    if len(body) < 40:
        raise HTTPException(400, "A sample needs to be a real email - at least a few sentences.")
    with db.conn() as c:
        c.execute("INSERT INTO voice_samples (body, created_at) VALUES (?,?)",
                  (body[:20000], db.now_iso()))
    return {"ok": True}


@app.delete("/api/voice/samples/{sample_id}")
def delete_sample(sample_id: int, request: Request):
    _require_header(request)
    with db.conn() as c:
        c.execute("DELETE FROM voice_samples WHERE id=?", (sample_id,))
    return {"ok": True}


@app.post("/api/voice/analyze")
def analyze_voice(request: Request):
    _require_header(request)
    try:
        profile = drafter.analyze_voice()
    except Exception as e:
        db.record_error("voice", str(e), traceback.format_exc())
        return JSONResponse({"ok": False, "message": str(e)[:300]}, status_code=502)
    config.update(voice_profile=profile)
    return {"ok": True, "profile": profile}


@app.post("/api/voice/profile")
async def save_profile(request: Request):
    _require_header(request)
    data = await request.json()
    config.update(voice_profile=(data.get("profile") or "").strip()[:4000])
    return {"ok": True}


# ------------------------------------------------------------- settings + ops

@app.post("/api/settings")
async def save_settings(request: Request):
    _require_header(request)
    data = await request.json()
    allowed = {"mode", "model", "signature_name", "tone_notes", "poll_interval", "gmail_address"}
    changes = {k: v for k, v in data.items() if k in allowed}
    if "notify_enabled" in data:
        changes["notify_enabled"] = bool(data["notify_enabled"])
    if "notify_cooldown_minutes" in data:
        changes["notify_cooldown_minutes"] = max(0, int(data["notify_cooldown_minutes"] or 30))
    if "followup_days" in data:
        changes["followup_days"] = max(0, int(data["followup_days"] or 0))
    if "quiet_start" in data:
        qs = (data.get("quiet_start") or "").strip()
        if qs and not timectl.valid_hhmm(qs):
            raise HTTPException(400, "Quiet start must be HH:MM (24-hour), or blank to turn off.")
        changes["quiet_start"] = qs
    if "quiet_end" in data:
        qe = (data.get("quiet_end") or "").strip()
        if qe and not timectl.valid_hhmm(qe):
            raise HTTPException(400, "Quiet end must be HH:MM (24-hour), or blank to turn off.")
        changes["quiet_end"] = qe
    for flag in ("vacation_mode", "feature_negotiation", "feature_rehearsal",
                 "feature_radar", "feature_parley", "negotiation_autodetect"):
        if flag in data:
            changes[flag] = bool(data[flag])
    if "parley_availability" in data:
        changes["parley_availability"] = (data.get("parley_availability") or "").strip()[:500]
    if "ignore_senders" in data:
        changes["ignore_senders"] = _split(data["ignore_senders"])
    if "only_senders" in data:
        changes["only_senders"] = _split(data["only_senders"])
    if "poll_interval" in changes:
        changes["poll_interval"] = max(60, int(changes["poll_interval"] or 300))
    if data.get("app_password"):
        pw = data["app_password"].replace(" ", "")
        config.set_secret("gmail_app_password", pw)
        # This field updates the PRIMARY inbox; other inboxes are managed in the
        # Inboxes section, which re-validates before storing.
        primary = (changes.get("gmail_address") or config.load().get("gmail_address") or "").strip()
        if primary:
            config.set_secret(config.gmail_secret_name(primary), pw)
    if data.get("api_key"):
        config.set_secret("anthropic_api_key", data["api_key"].strip())
    # Keep account 1 pointed at the primary address, or it silently keeps polling
    # the old inbox. Skipped if another inbox already holds that address.
    new_primary = (changes.get("gmail_address") or "").strip().lower()
    if new_primary:
        with db.conn() as c:
            clash = c.execute(
                "SELECT id FROM accounts WHERE address=? AND id != 1", (new_primary,)
            ).fetchone()
            if clash is None:
                c.execute(
                    "INSERT INTO accounts (id, address, label, created_at)"
                    " VALUES (1,?,?,?)"
                    " ON CONFLICT(id) DO UPDATE SET address=excluded.address",
                    (new_primary, "Primary", db.now_iso()),
                )
    config.update(**changes)
    return {"ok": True}


@app.post("/api/live")
async def set_live(request: Request):
    _require_header(request)
    data = await request.json()
    config.update(live_send=bool(data.get("on")))
    return {"ok": True, "live_send": bool(data.get("on"))}


@app.post("/api/poll-now")
def poll_now(request: Request):
    _require_header(request)
    if poll_loop:
        poll_loop.wake.set()
    return {"ok": True}


@app.post("/api/autostart")
async def set_autostart(request: Request):
    _require_header(request)
    data = await request.json()
    if data.get("install"):
        ok, msg = autostart.install()
        config.update(autostart_installed=ok)
    else:
        ok, msg = autostart.uninstall()
        config.update(autostart_installed=False)
    return {"ok": ok, "message": msg}


@app.post("/api/quit")
def quit_app(request: Request):
    _require_header(request)
    import os
    import threading as _t
    _t.Timer(0.5, lambda: os._exit(0)).start()
    return {"ok": True, "message": "MailPilot is shutting down."}
