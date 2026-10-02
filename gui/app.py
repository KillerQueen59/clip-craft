"""Native-window launcher for the ClipCraft GUI.

Starts the Flask server on a background thread, then opens a real desktop window
(pywebview / WebView2 on Windows) pointed at it — with a native menu bar and app
icon. A small JS-callable API exposes the OS file picker so "Browse…" can hand
back an absolute video path without uploading gigabytes through the browser.
"""

from __future__ import annotations

import os
import socket
import threading
import time
from pathlib import Path

import webview
from webview.menu import Menu, MenuAction, MenuSeparator

from server import PROJECTS_DIR, app

ASSETS = Path(__file__).resolve().parent / "assets"
ICON = ASSETS / "icon.ico"


def _free_port(preferred: int = 8765) -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        try:
            s.bind(("127.0.0.1", preferred))
            return preferred
        except OSError:
            s.bind(("127.0.0.1", 0))
            return s.getsockname()[1]


class Api:
    """Exposed to the page as `window.pywebview.api`."""

    def pick_video(self) -> str | None:
        types = ("Video files (*.mp4;*.mkv;*.mov;*.webm;*.m4v;*.avi)",
                 "All files (*.*)")
        result = webview.windows[0].create_file_dialog(
            webview.OPEN_DIALOG, allow_multiple=False, file_types=types)
        return result[0] if result else None


def _js(code: str) -> None:
    if webview.windows:
        webview.windows[0].evaluate_js(code)


def _open_projects_folder() -> None:
    PROJECTS_DIR.mkdir(exist_ok=True)
    os.startfile(str(PROJECTS_DIR))  # noqa: S606 - local path


def _build_menu() -> list[Menu]:
    return [
        Menu("File", [
            MenuAction("New Project\tCtrl+N", lambda: _js("document.getElementById('newProjectBtn').click()")),
            MenuAction("Open Projects Folder", _open_projects_folder),
            MenuSeparator(),
            MenuAction("Reload", lambda: _js("location.reload()")),
        ]),
        Menu("View", [
            MenuAction("Run tab", lambda: _js("document.querySelector('[data-tab=run]').click()")),
            MenuAction("Review tab", lambda: _js("document.querySelector('[data-tab=review]').click()")),
            MenuAction("Toggle console", lambda: _js("document.getElementById('consoleToggle').click()")),
        ]),
        Menu("Help", [
            MenuAction("About Clip System", lambda: _js(
                "typeof toast==='function' && toast('Clip System — desktop shell over the clipcraft pipeline');")),
        ]),
    ]


def main() -> None:
    port = _free_port()

    def serve():
        app.run(host="127.0.0.1", port=port, threaded=True,
                use_reloader=False, debug=False)

    threading.Thread(target=serve, daemon=True).start()

    for _ in range(50):  # wait for Flask to bind
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=0.1):
                break
        except OSError:
            time.sleep(0.05)

    webview.create_window(
        "Clip System",
        f"http://127.0.0.1:{port}/",
        js_api=Api(),
        width=1180,
        height=820,
        min_size=(920, 640),
    )
    webview.start(menu=_build_menu(), icon=str(ICON) if ICON.exists() else None)


if __name__ == "__main__":
    main()
