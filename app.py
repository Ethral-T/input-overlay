"""Input Overlay - tray app entry point (this is what the .exe runs).

Starts the capture/web server in a background thread and shows a system-tray icon:
  click / "Open settings"  -> settings page in your browser
  "Active preset"          -> switch which preset the base OBS URL shows
  "Copy OBS URL"           -> copies the base Browser Source URL
  "Open themes folder"     -> where you drop extra themes
  "Start with Windows"     -> toggles autostart for the current user
  "Quit"
"""
import argparse
import asyncio
import os
import socket
import subprocess
import sys
import threading
import time
import urllib.parse
import webbrowser
from pathlib import Path

import config

FROZEN = getattr(sys, "frozen", False)
APP_NAME = "InputOverlay"
RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"

# A windowed (no-console) exe has no stdout/stderr; send prints somewhere useful instead of crashing.
LOG_PATH = config.APP_DIR / "log.txt"
if sys.stdout is None or sys.stderr is None or FROZEN:
    config.APP_DIR.mkdir(parents=True, exist_ok=True)
    if LOG_PATH.exists() and LOG_PATH.stat().st_size > 512_000:
        LOG_PATH.write_text("", encoding="utf-8")
    sys.stdout = sys.stderr = open(LOG_PATH, "a", buffering=1, encoding="utf-8")
    print(f"\n--- {time.strftime('%Y-%m-%d %H:%M:%S')} start ---")

import server  # noqa: E402  (after stdout is sorted out)


def port_in_use(host, port):
    with socket.socket() as s:
        s.settimeout(0.3)
        return s.connect_ex((host, port)) == 0


def copy_to_clipboard(text):
    # URLs are plain ASCII once percent-encoded, so the built-in `clip` is enough (no extra dependency).
    clip = Path(os.environ.get("SystemRoot", r"C:\Windows")) / "System32" / "clip.exe"      # full path: never a look-alike found in another folder
    subprocess.run([str(clip)], input=text.encode("ascii", "ignore"), creationflags=subprocess.CREATE_NO_WINDOW, check=False)


# ---- autostart (HKCU Run key, current user only) -------------------------------------------------
def _autostart_command():
    if FROZEN:
        return f'"{sys.executable}"'
    pythonw = Path(sys.executable).with_name("pythonw.exe")
    return f'"{pythonw if pythonw.exists() else sys.executable}" "{Path(__file__).resolve()}"'


def autostart_enabled():
    import winreg
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY) as k:
            return winreg.QueryValueEx(k, APP_NAME)[0] == _autostart_command()
    except OSError:
        return False


def set_autostart(on):
    import winreg
    with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY, 0, winreg.KEY_SET_VALUE) as k:
        if on:
            winreg.SetValueEx(k, APP_NAME, 0, winreg.REG_SZ, _autostart_command())
        else:
            try:
                winreg.DeleteValue(k, APP_NAME)
            except FileNotFoundError:
                pass


# ---- main ----------------------------------------------------------------------------------------
def main():
    ap = argparse.ArgumentParser(description="Input Overlay")
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=8765)
    ap.add_argument("--pad", type=int, default=None, help="controller index when several are connected (default: first)")
    args = ap.parse_args()
    base = f"http://{args.host if args.host not in ('0.0.0.0', '::') else '127.0.0.1'}:{args.port}"

    first_run = not config.CONFIG_PATH.exists()

    if port_in_use(args.host, args.port):
        # Already running (or something else owns the port): just surface the settings page.
        print(f"port {args.port} already in use; opening settings")
        webbrowser.open(base + "/settings")
        return

    def run_server():
        try:
            asyncio.run(server.main(args))
        except Exception as e:  # noqa: BLE001
            print("server crashed:", repr(e))
            os._exit(1)

    threading.Thread(target=run_server, daemon=True).start()
    for _ in range(50):                       # wait for the port to come up
        if port_in_use(args.host, args.port):
            break
        time.sleep(0.1)

    import pystray
    from appicon import make_icon

    def open_settings(*_):
        webbrowser.open(base + "/settings")

    cfg = config.shared()                      # same object the server uses, so the menu is always current

    def active_items():
        for n in list(cfg.presets):
            yield pystray.MenuItem(n, lambda *_a, n=n: server.set_active(n), checked=lambda item, n=n: cfg.active == n, radio=True)

    def pinned_items():
        for n in list(cfg.presets):
            url = f"{base}/?preset={urllib.parse.quote(n)}"
            yield pystray.MenuItem(n, lambda *_a, u=url: copy_to_clipboard(u))

    def open_themes_folder(*_):
        os.startfile(str(server.themes.Themes(server.ROOT / "themes").ensure_user_dir()))

    def toggle_autostart(icon, item):
        set_autostart(not autostart_enabled())

    def quit_app(icon, item):
        icon.stop()
        os._exit(0)

    menu = pystray.Menu(
        pystray.MenuItem("Open settings", open_settings, default=True),
        pystray.MenuItem("Active preset", pystray.Menu(active_items)),
        pystray.MenuItem("Copy OBS URL", lambda *_: copy_to_clipboard(base + "/")),
        pystray.MenuItem("Copy URL pinned to a preset", pystray.Menu(pinned_items)),
        pystray.MenuItem("Open themes folder", open_themes_folder),
        pystray.Menu.SEPARATOR,
        pystray.MenuItem("Start with Windows", toggle_autostart, checked=lambda item: autostart_enabled()),
        pystray.MenuItem("Quit", quit_app),
    )
    icon = pystray.Icon(APP_NAME, make_icon(64), "Input Overlay", menu)
    def after_start(icon):
        icon.visible = True
        if first_run:
            open_settings()
        try:
            icon.notify("Running. Right-click the tray icon to open settings or copy an OBS URL.", "Input Overlay")
        except Exception:  # noqa: BLE001 - notifications are best-effort
            pass

    icon.run(after_start)


if __name__ == "__main__":
    main()
