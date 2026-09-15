"""MailPilot entry point.

Double-click (windowed): starts the server if it isn't already running, then
opens your browser to the app. Closing the browser tab does NOT stop MailPilot.

--headless: server only, no browser - what the autostart service runs.
"""
import argparse
import logging
import sys
import threading
import time
import urllib.request
import webbrowser

from mailpilot import config, paths


def server_alive(port: int) -> bool:
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/api/ping", timeout=2) as r:
            return b"mailpilot" in r.read()
    except Exception:
        return False


def run_server(port: int):
    import uvicorn
    from mailpilot.server import app
    uvicorn.run(app, host="127.0.0.1", port=port, log_level="warning")


def main():
    parser = argparse.ArgumentParser(prog="mailpilot")
    parser.add_argument("--headless", action="store_true", help="run the watcher/server only, no browser")
    parser.add_argument("--port", type=int, default=0)
    args = parser.parse_args()

    logging.basicConfig(
        filename=str(paths.log_path()), level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    port = args.port or int(config.load().get("ui_port") or 8765)
    url = f"http://127.0.0.1:{port}"

    if server_alive(port):
        if not args.headless:
            webbrowser.open(url)
        return

    if args.headless:
        run_server(port)
        return

    t = threading.Thread(target=run_server, args=(port,), daemon=True)
    t.start()
    for _ in range(100):
        if server_alive(port):
            break
        time.sleep(0.1)
    webbrowser.open(url)
    # Keep the process alive; Quit lives in the app's Settings screen.
    try:
        while t.is_alive():
            time.sleep(3600)
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    sys.exit(main())
