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
from fastapi.responses import FileResponse, JSONResponse

from . import VERSION, autostart, config, db, drafter, poller, sender

poll_loop: poller.PollLoop | None = None


def web_dir() -> Path:
    if getattr(sys, "frozen", False):
        return Path(sys._MEIPASS) / "web"  # type: ignore[attr-defined]
    return Path(__file__).resolve().parent.parent / "web"


@asynccontextmanager
async def lifespan(app: FastAPI):
    global poll_loop
    db.bootstrap()
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


@app.get("/api/state")
def state():
    cfg = config.load()
    return {
        "version": VERSION,
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
    path = drafter.find_claude_cli()
    return {"found": bool(path), "path": path}


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
        "claude_cli_path": drafter.find_claude_cli() if mode == "cli" else "",
    }
    if data.get("app_password"):
        config.set_secret("gmail_app_password", data["app_password"].replace(" ", ""))
    if mode == "api" and data.get("api_key"):
        config.set_secret("anthropic_api_key", data["api_key"].strip())
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


# ------------------------------------------------------------- queue + history

def _drafts_where(clause: str, params=()):
    with db.conn() as c:
        rows = c.execute(
            "SELECT d.id, d.body, d.status, d.block_reason, d.created_at, d.updated_at,"
            " d.sent_at, e.from_address, e.from_name, e.subject, e.body_text, e.received_at"
            f" FROM drafts d JOIN emails e ON e.id = d.email_id WHERE {clause}"
            " ORDER BY d.updated_at DESC LIMIT 200",
            params,
        ).fetchall()
    return [dict(r) for r in rows]


@app.get("/api/queue")
def queue():
    return {"drafts": _drafts_where("d.status='queued'")}


@app.get("/api/history")
def history():
    return {"drafts": _drafts_where("d.status IN ('sent','simulated','discarded','blocked')")}


@app.get("/api/skipped")
def skipped():
    with db.conn() as c:
        rows = c.execute(
            "SELECT id, from_address, from_name, subject, received_at, skip_reason"
            " FROM emails WHERE status='skipped' ORDER BY id DESC LIMIT 200"
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


# ------------------------------------------------------------- settings + ops

@app.post("/api/settings")
async def save_settings(request: Request):
    _require_header(request)
    data = await request.json()
    allowed = {"mode", "model", "signature_name", "tone_notes", "poll_interval", "gmail_address"}
    changes = {k: v for k, v in data.items() if k in allowed}
    if "ignore_senders" in data:
        changes["ignore_senders"] = _split(data["ignore_senders"])
    if "only_senders" in data:
        changes["only_senders"] = _split(data["only_senders"])
    if "poll_interval" in changes:
        changes["poll_interval"] = max(60, int(changes["poll_interval"] or 300))
    if data.get("app_password"):
        config.set_secret("gmail_app_password", data["app_password"].replace(" ", ""))
    if data.get("api_key"):
        config.set_secret("anthropic_api_key", data["api_key"].strip())
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
