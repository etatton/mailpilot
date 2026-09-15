"""Per-OS locations for MailPilot's data. Nothing lives in the install directory:
a packaged app may sit somewhere read-only, so config, DB, and logs go to the
user's application-data area.
"""
import os
import sys
from pathlib import Path


def app_data_dir() -> Path:
    if sys.platform == "win32":
        base = Path(os.environ.get("APPDATA", Path.home() / "AppData" / "Roaming"))
        d = base / "MailPilot"
    elif sys.platform == "darwin":
        d = Path.home() / "Library" / "Application Support" / "MailPilot"
    else:
        d = Path(os.environ.get("XDG_DATA_HOME", Path.home() / ".local" / "share")) / "mailpilot"
    d.mkdir(parents=True, exist_ok=True)
    return d


def db_path() -> Path:
    return app_data_dir() / "mailpilot.db"


def config_path() -> Path:
    return app_data_dir() / "config.json"


def log_path() -> Path:
    return app_data_dir() / "mailpilot.log"


def executable_command() -> list[str]:
    """The command that relaunches this same app — used by autostart installers.
    Frozen (PyInstaller): the bundled executable. From source: python + app.py.
    """
    if getattr(sys, "frozen", False):
        return [sys.executable]
    return [sys.executable, str(Path(__file__).resolve().parent.parent / "app.py")]
