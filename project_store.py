"""Multi-project library.

The app runs one live catalog at data/catalog.db. Each project keeps its own
copy under data/projects/<id>/catalog.db, and a registry (data/projects.json)
tracks name / dates / whether a film was generated ("finished"). Switching
projects copies content in and out of the live catalog via SQLite's online
backup API - the same mechanism New-trip already relies on, which works while
the DB is in use (no fragile file moves on Windows).

Enrolled people (faces) are SHARED across all projects: on every switch the
live people table is snapshotted and re-applied after the target catalog is
loaded, so a family enrolled once is available in every trip.
"""

import json
import re
import shutil
import sqlite3
import uuid
from datetime import datetime
from pathlib import Path

import config
from db.schema import DEFAULT_DB_PATH, get_connection, init_db

# Characters not allowed in Windows filenames; stripped when a project's name
# becomes its video's filename.
_FS_UNSAFE = re.compile(r'[\\/:*?"<>|]+')

PROJECTS_DIR = Path("data") / "projects"
REGISTRY_PATH = Path("data") / "projects.json"
ARCHIVE_DIR = Path("data") / "archive"
_PEOPLE_COLS = ["person_label", "embedding", "reference_photo_path", "created_at"]


# ---- registry ----
def _load_registry() -> list[dict]:
    if REGISTRY_PATH.is_file():
        try:
            return json.loads(REGISTRY_PATH.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            return []
    return []


def _save_registry(rows: list[dict]) -> None:
    REGISTRY_PATH.parent.mkdir(parents=True, exist_ok=True)
    REGISTRY_PATH.write_text(json.dumps(rows, indent=2, ensure_ascii=False), encoding="utf-8")


def _touch(pid: str, **fields) -> None:
    rows = _load_registry()
    for r in rows:
        if r["id"] == pid:
            r["updated"] = _now()
            r.update(fields)
    _save_registry(rows)


def list_projects() -> list[dict]:
    return sorted(_load_registry(), key=lambda r: r.get("updated", ""), reverse=True)


def get_project(pid: str | None) -> dict | None:
    return next((r for r in _load_registry() if r["id"] == pid), None) if pid else None


def _archive_when(ts: str) -> str:
    """Turn a 'YYYYMMDD_HHMMSS' archive folder name into an ISO timestamp."""
    try:
        return datetime.strptime(ts, "%Y%m%d_%H%M%S").isoformat(timespec="seconds")
    except ValueError:
        return ts


def _archive_meta(folder: Path, db: Path) -> tuple[str, str | None]:
    """Read a display name (first line of the trip intro) and any rendered film
    from an archived trip folder."""
    name = ""
    try:
        conn = get_connection(db)
        try:
            row = conn.execute("SELECT intro_text FROM trip_intro WHERE id = 1").fetchone()
        finally:
            conn.close()
        if row and row["intro_text"]:
            name = row["intro_text"].splitlines()[0].strip()
    except sqlite3.Error:
        pass
    out_dir = folder / "output"
    videos = sorted(out_dir.glob("*.mp4")) if out_dir.is_dir() else []
    fallback = f"Archived trip · {_archive_when(folder.name)[:10]}"
    return (name or fallback, videos[-1].name if videos else None)


def list_archived() -> list[dict]:
    """Trips set aside by the old 'New trip' flow (data/archive/<ts>/catalog.db),
    which predate the multi-project registry. Shown read-only until restored."""
    out: list[dict] = []
    if not ARCHIVE_DIR.is_dir():
        return out
    for folder in sorted(ARCHIVE_DIR.iterdir(), key=lambda p: p.name, reverse=True):
        db = folder / "catalog.db"
        if not db.is_file() or (folder / ".restored").exists():
            continue
        name, video = _archive_meta(folder, db)
        out.append({"id": folder.name, "name": name, "updated": _archive_when(folder.name),
                    "video": video, "archived": True})
    return out


def restore_archive(ts: str) -> str | None:
    """Import an old 'New trip' archive as a live project and switch to it.
    Non-destructive: the archive folder is kept, only marked '.restored' so it
    drops out of the archived list. Returns the new project id, or None."""
    folder = ARCHIVE_DIR / ts
    src = folder / "catalog.db"
    if not src.is_file() or (folder / ".restored").exists():
        return None
    name, _video = _archive_meta(folder, src)
    save_current()                       # preserve the project we're leaving
    people = _snapshot_people()          # people are shared across projects
    _backup(src, DEFAULT_DB_PATH)        # the archive becomes the live catalog
    init_db(DEFAULT_DB_PATH)             # archives predate newer columns - migrate
    _restore_people(people)
    pid = _new_id()
    now = _now()
    rows = _load_registry()
    rows.append({"id": pid, "name": name, "created": now, "updated": now, "video": None})
    _save_registry(rows)
    config.set_setting("current_project", pid)
    save_current()                       # persist the migrated catalog into the project file
    try:
        (folder / ".restored").write_text(pid, encoding="utf-8")
    except OSError:
        pass
    return pid


def last_unfinished() -> dict | None:
    """Most-recently-touched project that hasn't produced a film yet."""
    return next((r for r in list_projects() if not r.get("video")), None)


def current_id() -> str | None:
    return config.get_setting("current_project", None)


def current() -> dict | None:
    return get_project(current_id())


def output_basename() -> str:
    """Filesystem-safe stem for the rendered video, derived from the CURRENT
    project's name - so each project's film is named after itself, even when a
    finished project is reopened and re-rendered.

    This deliberately does NOT use the global `output_basename` setting as the
    primary source: that setting is app-wide and survives project switches, so it
    reflected whichever project last set it (the bug where a reopened project's
    video took the in-work project's name). The setting is only a fallback for
    when there is no current project or the name has no usable characters."""
    proj = current()
    name = (proj or {}).get("name", "") or ""
    slug = _FS_UNSAFE.sub("", name)
    slug = re.sub(r"\s+", "_", slug).strip("_. ")[:60]
    if slug:
        return slug
    return config.get_setting("output_basename", "trip_video") or "trip_video"


# ---- helpers ----
def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


def _new_id() -> str:
    return uuid.uuid4().hex[:12]


def _project_db(pid: str) -> Path:
    return PROJECTS_DIR / pid / "catalog.db"


def _live_item_count() -> int:
    conn = get_connection(DEFAULT_DB_PATH)
    try:
        return conn.execute("SELECT COUNT(*) AS n FROM items").fetchone()["n"]
    finally:
        conn.close()


def _backup(src_path: Path, dst_path: Path) -> None:
    """Copy the whole DB from src_path into dst_path via the online backup API."""
    dst_path.parent.mkdir(parents=True, exist_ok=True)
    src = get_connection(src_path)
    dst = get_connection(dst_path)
    try:
        src.backup(dst)
        dst.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    finally:
        dst.close()
        src.close()


def _snapshot_people() -> list[tuple]:
    conn = get_connection(DEFAULT_DB_PATH)
    try:
        return [tuple(r) for r in conn.execute(
            f"SELECT {', '.join(_PEOPLE_COLS)} FROM enrolled_faces"
        ).fetchall()]
    finally:
        conn.close()


def _restore_people(rows: list[tuple]) -> None:
    conn = get_connection(DEFAULT_DB_PATH)
    try:
        conn.execute("DELETE FROM enrolled_faces")
        placeholders = ", ".join(["?"] * len(_PEOPLE_COLS))
        conn.executemany(
            f"INSERT INTO enrolled_faces ({', '.join(_PEOPLE_COLS)}) VALUES ({placeholders})", rows
        )
        conn.commit()
    finally:
        conn.close()


def _clear_live_keep_people() -> None:
    """Empty the live catalog for a fresh project - keep enrolled people."""
    conn = get_connection(DEFAULT_DB_PATH)
    try:
        conn.execute("DELETE FROM items")
        conn.execute("DELETE FROM clusters")
        conn.execute("DELETE FROM skipped_files")
        conn.execute("DELETE FROM day_captions")
        conn.execute("UPDATE trip_intro SET intro_text = '', outro_text = '' WHERE id = 1")
        conn.commit()
    finally:
        conn.close()


# ---- operations ----
def save_current() -> None:
    """Persist the live catalog back into the current project's file."""
    cid = current_id()
    if not cid:
        return
    _backup(DEFAULT_DB_PATH, _project_db(cid))
    _touch(cid)


def adopt_current(name: str) -> str:
    """Register the existing live catalog as a project (first-run migration)."""
    pid = _new_id()
    now = _now()
    rows = _load_registry()
    rows.append({"id": pid, "name": name, "created": now, "updated": now, "video": None})
    _save_registry(rows)
    config.set_setting("current_project", pid)
    save_current()
    return pid


def ensure_current() -> None:
    """If there's no current project but the live catalog already holds a trip,
    adopt it so the existing work becomes the first project."""
    if current_id():
        return
    if _live_item_count() > 0:
        base = (config.get_setting("output_basename", "trip") or "trip")
        adopt_current(base.replace("_", " ").replace("-", " ").title() or "My trip")


def create_project(name: str) -> str:
    """Save the current project, then start a fresh empty one (people kept)."""
    save_current()
    _clear_live_keep_people()
    pid = _new_id()
    now = _now()
    rows = _load_registry()
    rows.append({"id": pid, "name": (name or "").strip() or "Untitled trip",
                 "created": now, "updated": now, "video": None})
    _save_registry(rows)
    config.set_setting("current_project", pid)
    save_current()
    return pid


def load_project(pid: str) -> bool:
    """Make an existing project the live one. Returns False if unknown."""
    if pid == current_id():
        return True
    if get_project(pid) is None or not _project_db(pid).is_file():
        return False
    save_current()
    people = _snapshot_people()
    _backup(_project_db(pid), DEFAULT_DB_PATH)
    init_db(DEFAULT_DB_PATH)  # bring older project files (e.g. restored archives) up to date
    _restore_people(people)
    config.set_setting("current_project", pid)
    _touch(pid)
    return True


def rename_project(pid: str, name: str) -> None:
    _touch(pid, name=name.strip() or "Untitled trip")


def delete_project(pid: str) -> None:
    _save_registry([r for r in _load_registry() if r["id"] != pid])
    shutil.rmtree(PROJECTS_DIR / pid, ignore_errors=True)
    if current_id() == pid:
        config.set_setting("current_project", "")


def mark_video(video_name: str) -> None:
    """Record that the current project produced a film (-> 'finished')."""
    cid = current_id()
    if cid:
        _touch(cid, video=video_name)
