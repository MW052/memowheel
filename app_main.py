"""Frozen-app entry point (PyInstaller): run the app windowless with a system-tray
icon (Open / View log / Quit) - the double-clickable, no-console equivalent of
`manage.py serve`.

The real work lives in `tray_app.main()`, which is shared with the dev command
`py manage.py tray`. It handles: freeze_support() for the multiprocessing render
workers, chdir to the executable's folder (so the writable `data/` dir is created
next to the app, not inside the read-only bundle), redirecting stdout/stderr to
the log file (a windowed exe has no real console), starting uvicorn, and the tray
loop. Keeping this file a thin delegate means the frozen build and the dev tray
command can never drift apart.
"""

import tray_app


def main() -> None:
    tray_app.main()


if __name__ == "__main__":
    main()
