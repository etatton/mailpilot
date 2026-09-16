"""Configuration store.

Non-secret settings live in config.json under the app-data dir. Secrets (the
Gmail app password, the Anthropic API key) go to the OS credential store via
`keyring` — Windows Credential Manager / macOS Keychain. If keyring is
unavailable the secret falls back to config.json and `keyring_ok` flips false
so the UI can say so; the app keeps working either way.

Secret VALUES are never logged. Log presence/length only.
"""
import json
import threading

from . import paths

_LOCK = threading.Lock()
_SERVICE = "MailPilot"

DEFAULTS = {
    "configured": False,
    "mode": "api",                # "api" (Anthropic API key) | "cli" (local Claude Code install)
    "model": "claude-opus-5",     # api mode only
    "gmail_address": "",
    "signature_name": "",
    "tone_notes": "",
    "voice_profile": "",
    "ignore_senders": [],
    "only_senders": [],
    "poll_interval": 300,
    "live_send": False,
    "ui_port": 8765,
    "autostart_installed": False,
    "notify_enabled": True,
    "notify_cooldown_minutes": 30,
    "followup_days": 3,            # 0 disables follow-up nudges
    "claude_cli_path": "",
    "keyring_ok": True,
}

_SECRET_KEYS = ("gmail_app_password", "anthropic_api_key")


def load() -> dict:
    cfg = dict(DEFAULTS)
    p = paths.config_path()
    if p.exists():
        try:
            cfg.update(json.loads(p.read_text(encoding="utf-8")))
        except (json.JSONDecodeError, OSError):
            pass
    return cfg


def save(cfg: dict) -> None:
    clean = {k: v for k, v in cfg.items() if k in DEFAULTS or k in _SECRET_KEYS}
    with _LOCK:
        paths.config_path().write_text(
            json.dumps(clean, indent=2), encoding="utf-8"
        )


def update(**changes) -> dict:
    cfg = load()
    cfg.update(changes)
    save(cfg)
    return cfg


def set_secret(name: str, value: str) -> bool:
    """Store a secret. Returns True if it landed in the OS credential store."""
    assert name in _SECRET_KEYS
    try:
        import keyring
        keyring.set_password(_SERVICE, name, value)
        # Remove any earlier plaintext fallback
        cfg = load()
        if name in cfg:
            cfg.pop(name, None)
            cfg["keyring_ok"] = True
            save(cfg)
        else:
            update(keyring_ok=True)
        return True
    except Exception:
        update(**{name: value, "keyring_ok": False})
        return False


def get_secret(name: str) -> str:
    assert name in _SECRET_KEYS
    try:
        import keyring
        v = keyring.get_password(_SERVICE, name)
        if v:
            return v
    except Exception:
        pass
    return load().get(name, "") or ""


def delete_secret(name: str) -> None:
    assert name in _SECRET_KEYS
    try:
        import keyring
        keyring.delete_password(_SERVICE, name)
    except Exception:
        pass
    cfg = load()
    if name in cfg:
        cfg.pop(name, None)
        save(cfg)
