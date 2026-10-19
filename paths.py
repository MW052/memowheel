"""Resource-path resolution + the writable user-data location.

Two different roots:

- **Read-only bundled assets** (templates, static, fonts, models) -> `resource_path`,
  which points at the source tree in dev and the PyInstaller extraction dir
  (`sys._MEIPASS`) when frozen.
- **Writable user data** (catalog.db, settings.json, projects, reference faces,
  music, output, logs) -> `data_dir()` / `data_path()`. This is kept SEPARATE
  from the program folder so the app can be updated by just replacing the program
  files, without touching the user's data.

Resolution of the data root (deterministic - never depends on which folders
happen to exist, so module-level path constants are valid no matter when they're
computed):

  1. `$MEMOWHEEL_DATA_DIR`, if set - an explicit override (tests, power users).
  2. Portable mode: a `portable.txt` marker next to the app -> `<app>/data`
     (keeps everything in one folder, e.g. on a USB stick; nothing touches the
     system).
  3. Dev (not frozen): `./data` relative to the working dir - unchanged from how
     the app has always run from source.
  4. Frozen default: `%LOCALAPPDATA%/Memowheel/data` (Windows), else
     `~/.memowheel/data`.

`migrate_legacy_data_if_needed()` moves a pre-relocation `data/` folder (which
older frozen builds wrote next to the exe) into the new per-user location on
first run, so upgrading users keep their projects/faces/settings.
"""

import os
import shutil
import sys
from pathlib import Path

APP_NAME = "Memowheel"


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


def _app_folder() -> Path:
    """The folder the app runs from: next to the exe when frozen, else the cwd."""
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path.cwd()


def _is_portable() -> bool:
    return (_app_folder() / "portable.txt").is_file()


def _resolve_data_dir() -> Path:
    """The writable-data root. Pure: computes a path, never touches the disk."""
    override = os.environ.get("MEMOWHEEL_DATA_DIR")
    if override:
        return Path(override)
    if _is_portable():
        return _app_folder() / "data"
    if not getattr(sys, "frozen", False):
        return Path("data")  # dev: cwd-relative, exactly as before
    local = os.environ.get("LOCALAPPDATA") or str(Path.home() / "AppData" / "Local")
    return Path(local) / APP_NAME / "data"


def data_dir() -> Path:
    """Writable root for ALL user data (catalog, projects, settings, reference
    faces, music, output, logs). See the module docstring for how it resolves."""
    return _resolve_data_dir()


def data_path(*parts: str) -> Path:
    """Convenience: data_dir() joined with sub-parts, e.g. data_path('catalog.db')."""
    return data_dir().joinpath(*parts)


def ensure_data_dir() -> Path:
    """data_dir(), guaranteed to exist on disk."""
    d = data_dir()
    d.mkdir(parents=True, exist_ok=True)
    return d


def migrate_legacy_data_if_needed() -> None:
    """One-time: older frozen builds wrote `data/` next to the exe. If such a
    legacy folder exists and the new per-user location has not been populated yet,
    move it so upgrading users keep their data without any manual copying.

    No-op in dev, in portable mode, with an explicit override, when there's no
    legacy data, or when the new location already exists. Must run BEFORE anything
    reads or writes user data (the caller runs it at startup). Never raises - a
    migration problem must not stop the app from launching; the legacy folder is
    left intact if the move fails."""
    if not getattr(sys, "frozen", False):
        return
    if _is_portable() or os.environ.get("MEMOWHEEL_DATA_DIR"):
        return
    legacy = _app_folder() / "data"
    target = _resolve_data_dir()
    try:
        if not legacy.is_dir():
            return
        if legacy.resolve() == target.resolve():
            return
        if target.exists():
            return  # new location already populated; don't clobber it
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(legacy), str(target))
    except Exception:
        # Leave the legacy folder where it is; the user still has their data and
        # can set a portable.txt marker to keep using the old location.
        pass
