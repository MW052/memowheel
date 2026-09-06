"""Windowless launcher: run the review server with NO console window and put a
small icon in the system tray (Open / View log / Quit).

This is the default way a non-technical user runs the app - double-clicking a
launcher that calls this via `pythonw` (or the frozen windowed exe) shows no
terminal at all. Because there's no console, logs go to data/logs/tripvideo.log
(see runtime_log), viewable from the app's kebab menu -> "View log".

Run directly for development:  py tray_app.py   (or: py manage.py tray)
"""

import os
import sys
import threading
import webbrowser
from pathlib import Path

URL = "http://127.0.0.1:8000/"


def _icon_image():
    """A simple terracotta disc with a white play triangle - drawn with Pillow
    (already a dependency) so we don't ship an .ico."""
    from PIL import Image, ImageDraw

    img = Image.new("RGBA", (64, 64), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)
    draw.ellipse((3, 3, 61, 61), fill=(192, 57, 43, 255))       # brand terracotta
    draw.polygon([(26, 20), (26, 44), (46, 32)], fill=(255, 255, 255, 255))
    return img


def main() -> None:
    # A windowed/frozen exe (and pythonw) has sys.stdout/stderr == None. The
    # parallel render fans out with multiprocessing spawn, and each worker
    # re-enters this function via freeze_support() BEFORE we redirect streams
    # below - so guard None streams first, or a worker writing to stdout/stderr
    # (multiprocessing bootstrap, a stray print) would crash with AttributeError.
    if sys.stdout is None or sys.stderr is None:
        devnull = open(os.devnull, "w")
        if sys.stdout is None:
            sys.stdout = devnull
        if sys.stderr is None:
            sys.stderr = devnull

    # multiprocessing spawn (parallel render) re-launches this process; guard so
    # workers don't boot a second server/tray. Must run before anything heavy.
    import multiprocessing
    multiprocessing.freeze_support()

    if getattr(sys, "frozen", False):
        os.chdir(Path(sys.executable).resolve().parent)

    from runtime_log import build_log_config, redirect_streams_to_log
    # No console here (pythonw/frozen): capture prints + tracebacks to the log.
    redirect_streams_to_log()

    import uvicorn
    from review_app.main import app

    config = uvicorn.Config(
        app, host="127.0.0.1", port=8000,
        log_config=build_log_config(console=False),
        timeout_graceful_shutdown=5,
    )
    server = uvicorn.Server(config)
    threading.Thread(target=server.run, daemon=True).start()

    if not os.environ.get("TRIPVIDEO_NO_BROWSER"):
        threading.Timer(2.0, lambda: webbrowser.open(URL)).start()

    import pystray
    from pystray import Menu, MenuItem

    def on_open(icon, item):
        webbrowser.open(URL)

    def on_view_log(icon, item):
        webbrowser.open(URL + "logs")

    def on_quit(icon, item):
        server.should_exit = True  # ask uvicorn to shut down cleanly
        icon.stop()                # end the tray loop -> main() returns

    icon = pystray.Icon(
        "tripvideo",
        _icon_image(),
        "Trip Video",
        menu=Menu(
            MenuItem("Open Trip Video", on_open, default=True),
            MenuItem("View log", on_view_log),
            MenuItem("Quit", on_quit),
        ),
    )
    icon.run()  # blocks on the tray message loop until Quit


if __name__ == "__main__":
    main()
