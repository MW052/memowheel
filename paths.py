"""Resource-path resolution that works both from source and inside a frozen
(PyInstaller) build.

Read-only bundled assets (templates, static files, fonts) are looked up via
`resource_path`, which points at the source tree in dev and at the PyInstaller
extraction dir (`sys._MEIPASS`) when frozen. Writable user data (the `data/`
folder: catalog.db, settings.json, music, output, ...) stays *relative to the
current working directory* - the frozen entry point (`app_main.py`) chdir's to
the app folder so that folder holds the user's data, while `manage.py` in dev
keeps using the project root. So no data-path changes are needed here.
"""

import sys
from pathlib import Path


def _base() -> Path:
    if getattr(sys, "frozen", False):
        # PyInstaller: bundled datas live under _MEIPASS (onefile) or next to the
        # executable's _internal dir (onedir); _MEIPASS covers both.
        return Path(getattr(sys, "_MEIPASS", Path(sys.executable).resolve().parent))
    return Path(__file__).resolve().parent  # project root (this file lives there)


def resource_path(relative: str) -> Path:
    """Absolute path to a bundled read-only asset, e.g.
    resource_path("review_app/templates")."""
    return _base() / relative
