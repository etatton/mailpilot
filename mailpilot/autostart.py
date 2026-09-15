"""Start-at-login + restart-on-crash, per platform.

The service manager owns crash recovery - the app has no supervisor loop:
- Windows: a Scheduled Task (logon trigger, RestartOnFailure every minute).
- macOS: a launchd LaunchAgent (RunAtLoad, KeepAlive on non-clean exit).
- Linux (running from source): a systemd --user unit (Restart=on-failure).
"""
import plistlib
import subprocess
import sys
from pathlib import Path

from . import paths

TASK_NAME = "MailPilot"
MAC_LABEL = "com.mailpilot.app"

_WIN_TASK_XML = """<?xml version="1.0" encoding="UTF-16"?>
<Task version="1.4" xmlns="http://schemas.microsoft.com/windows/2004/02/mit/task">
  <RegistrationInfo><Description>MailPilot inbox watcher</Description></RegistrationInfo>
  <Triggers><LogonTrigger><Enabled>true</Enabled></LogonTrigger></Triggers>
  <Settings>
    <DisallowStartIfOnBatteries>false</DisallowStartIfOnBatteries>
    <StopIfGoingOnBatteries>false</StopIfGoingOnBatteries>
    <StartWhenAvailable>true</StartWhenAvailable>
    <ExecutionTimeLimit>PT0S</ExecutionTimeLimit>
    <RestartOnFailure><Interval>PT1M</Interval><Count>99</Count></RestartOnFailure>
  </Settings>
  <Actions Context="Author">
    <Exec><Command>{cmd}</Command><Arguments>{args}</Arguments></Exec>
  </Actions>
</Task>
"""

_SYSTEMD_UNIT = """[Unit]
Description=MailPilot inbox watcher

[Service]
ExecStart={exec_start}
Restart=on-failure
RestartSec=5

[Install]
WantedBy=default.target
"""


def _command() -> list[str]:
    return paths.executable_command() + ["--headless"]


def install() -> tuple[bool, str]:
    cmd = _command()
    try:
        if sys.platform == "win32":
            xml = _WIN_TASK_XML.format(
                cmd=cmd[0],
                args=" ".join(f'"{a}"' if " " in a else a for a in cmd[1:]),
            )
            xml_file = paths.app_data_dir() / "mailpilot-task.xml"
            xml_file.write_text(xml, encoding="utf-16")
            r = subprocess.run(
                ["schtasks", "/Create", "/TN", TASK_NAME, "/XML", str(xml_file), "/F"],
                capture_output=True, text=True, creationflags=0x08000000,
            )
            if r.returncode != 0:
                return False, f"schtasks failed: {(r.stderr or r.stdout).strip()[:300]}"
            return True, "Installed as a Windows scheduled task (starts at sign-in, restarts on crash)."

        if sys.platform == "darwin":
            plist = {
                "Label": MAC_LABEL,
                "ProgramArguments": cmd,
                "RunAtLoad": True,
                "KeepAlive": {"SuccessfulExit": False},
                "StandardOutPath": str(paths.log_path()),
                "StandardErrorPath": str(paths.log_path()),
            }
            agents = Path.home() / "Library" / "LaunchAgents"
            agents.mkdir(parents=True, exist_ok=True)
            plist_path = agents / f"{MAC_LABEL}.plist"
            with open(plist_path, "wb") as f:
                plistlib.dump(plist, f)
            uid = subprocess.run(["id", "-u"], capture_output=True, text=True).stdout.strip()
            r = subprocess.run(
                ["launchctl", "bootstrap", f"gui/{uid}", str(plist_path)],
                capture_output=True, text=True,
            )
            if r.returncode != 0:
                r2 = subprocess.run(["launchctl", "load", "-w", str(plist_path)],
                                    capture_output=True, text=True)
                if r2.returncode != 0:
                    return False, f"launchctl failed: {(r.stderr or r2.stderr).strip()[:300]}"
            return True, "Installed as a macOS login item (starts at login, relaunches on crash)."

        # Linux / running from source
        unit_dir = Path.home() / ".config" / "systemd" / "user"
        unit_dir.mkdir(parents=True, exist_ok=True)
        (unit_dir / "mailpilot.service").write_text(
            _SYSTEMD_UNIT.format(exec_start=" ".join(cmd)), encoding="utf-8"
        )
        for args in (["daemon-reload"], ["enable", "--now", "mailpilot"]):
            r = subprocess.run(["systemctl", "--user"] + args, capture_output=True, text=True)
            if r.returncode != 0:
                return False, f"systemctl {args[0]} failed: {r.stderr.strip()[:300]}"
        return True, "Installed as a systemd user service (starts at login, restarts on crash)."
    except Exception as e:
        return False, f"Autostart install failed: {e}"


def uninstall() -> tuple[bool, str]:
    try:
        if sys.platform == "win32":
            subprocess.run(["schtasks", "/Delete", "/TN", TASK_NAME, "/F"],
                           capture_output=True, text=True, creationflags=0x08000000)
        elif sys.platform == "darwin":
            uid = subprocess.run(["id", "-u"], capture_output=True, text=True).stdout.strip()
            plist_path = Path.home() / "Library" / "LaunchAgents" / f"{MAC_LABEL}.plist"
            subprocess.run(["launchctl", "bootout", f"gui/{uid}/{MAC_LABEL}"],
                           capture_output=True, text=True)
            plist_path.unlink(missing_ok=True)
        else:
            subprocess.run(["systemctl", "--user", "disable", "--now", "mailpilot"],
                           capture_output=True, text=True)
            (Path.home() / ".config" / "systemd" / "user" / "mailpilot.service").unlink(missing_ok=True)
        return True, "Autostart removed."
    except Exception as e:
        return False, f"Autostart removal failed: {e}"
