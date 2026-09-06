"""Onboarding / setup wizard (Phase 3): the whole flow a non-technical person
needs before review - enroll people, configure the LLM + folders, and import a
trip folder - all in the browser, replacing the CLI (`manage.py enroll/ingest`).

Single-user local app, so the long import runs in a background thread with a
simple in-memory status the UI polls; "New trip" archives the current catalog
aside (keeping enrolled people) and starts fresh.
"""

import json
import os
import re
import shutil
import threading
from datetime import date, datetime
from pathlib import Path, PurePosixPath

from fastapi import APIRouter, Depends, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, Response

import config
import secret_store
from cloud.llm import test_llm
from db.schema import get_connection, DEFAULT_DB_PATH
from ingest.clustering import DEFAULT_GAP_THRESHOLD_MINUTES
from ingest.faces import assess_enrollment, save_enrollment, load_enrolled
from ingest.dates import media_capture_date
from ingest.itinerary import parse_itinerary
from ingest.pipeline import run_ingest, _iter_media_files, classify_media, PHOTO_EXTENSIONS
from review_app.db import get_db
from review_app.templating import templates
import project_store

router = APIRouter()

REFERENCE_DIR = Path("data") / "reference"
DATA_DIR = Path("data")

# --- import job (single-user local app: one at a time, in-memory status) ---
_import_lock = threading.Lock()
_import_status = {
    "state": "idle", "processed": 0, "total": 0, "message": "",
    # Date-filter outcome, surfaced on the finish card:
    "date_filtered": False, "skipped_out_of_range": 0, "included_undated": 0,
}


def _set_status(**kw):
    with _import_lock:
        _import_status.update(kw)


def _get_status() -> dict:
    with _import_lock:
        return dict(_import_status)


def _safe_name(name: str) -> str:
    """Folder-safe version of a person's name (the raw name stays the label)."""
    cleaned = re.sub(r"[^\w\-. ]", "_", name).strip()
    return cleaned or "person"


def _remove_files(paths: list[str]) -> None:
    """Best-effort delete of just-uploaded reference files (used when an
    enrollment is rejected, so nothing lingers on disk)."""
    for p in paths:
        try:
            Path(p).unlink(missing_ok=True)
        except OSError:
            pass


# Each Film-mood preset is exactly one (video_style, motion_style, seconds) triple;
# kept in sync with PRESETS in setup.html. Used to highlight the matching card.
_MOOD_PRESETS = {
    "keepsake": ("keepsake", "fade", 4.0),
    "modern": ("minimal", "static", 3.5),
    "lively": ("espresso", "ken_burns", 2.5),
}


def _match_mood_preset(settings: dict) -> str:
    try:
        secs = float(settings.get("photo_duration_seconds"))
    except (TypeError, ValueError):
        secs = None
    for name, (vs, ms, sec) in _MOOD_PRESETS.items():
        if settings.get("video_style") == vs and settings.get("motion_style") == ms and secs == sec:
            return name
    return "custom"


@router.get("/setup", response_class=HTMLResponse)
def setup_page(request: Request, conn=Depends(get_db)):
    people = [
        dict(r)
        for r in conn.execute(
            "SELECT person_label, COUNT(*) AS n, MIN(reference_photo_path) AS refs "
            "FROM enrolled_faces GROUP BY person_label ORDER BY person_label"
        ).fetchall()
    ]
    item_count = conn.execute("SELECT COUNT(*) AS n FROM items").fetchone()["n"]

    settings = config.all_settings()
    settings["llm_api_key_set"] = secret_store.has_api_key()
    settings["llm_api_key"] = ""  # never render the key back to the page
    settings["key_storage_mode"] = secret_store.storage_mode()  # env|keychain|plaintext|none
    settings["auto_place_id_on"] = config.get_bool("auto_place_id", False)
    # Highlight the mood preset that actually matches the saved look/motion/seconds
    # so an existing user with hand-tuned values sees "custom" (nothing selected),
    # not a preset that lies about what the video will look like.
    settings["mood_preset"] = _match_mood_preset(settings)

    music_tracks = [p.name for p in config.ordered_music_files(config.get_setting("music_dir"))]

    home = Path.home()
    default_start = home / "Pictures"
    return templates.TemplateResponse(request, "setup.html", {
        "people": people,
        "item_count": item_count,
        "settings": settings,
        "music_tracks": music_tracks,
        "gap_default": DEFAULT_GAP_THRESHOLD_MINUTES,
        "default_start": str(default_start if default_start.is_dir() else home),
        "import_status": _get_status(),
    })


@router.post("/setup/people")
async def add_person(
    name: str = Form(...),
    photos: list[UploadFile] = File(...),
    conn=Depends(get_db),
):
    name = name.strip()
    if not name:
        raise HTTPException(status_code=400, detail="A name is required.")
    person_dir = REFERENCE_DIR / _safe_name(name)
    person_dir.mkdir(parents=True, exist_ok=True)

    saved: list[str] = []
    for up in photos:
        if not up.filename:
            continue
        dest = person_dir / Path(up.filename).name
        dest.write_bytes(await up.read())
        saved.append(str(dest))
    if not saved:
        raise HTTPException(status_code=400, detail="Upload at least one reference photo.")

    try:
        quality = assess_enrollment(saved)
    except ValueError:
        _remove_files(saved)
        raise HTTPException(
            status_code=400,
            detail=("Couldn't find a face in any of those photos. Use clear, "
                    "front-facing photos of just this person."),
        )

    result = {"ok": True, "person": name, **quality.as_dict()}
    if not quality.accepted:
        # The photos don't agree with each other - don't enroll a muddled
        # embedding. Discard the just-uploaded files so a retry starts clean and
        # any existing good enrollment for this person is left untouched.
        _remove_files(saved)
        result["ok"] = False
        return result

    save_enrollment(conn, name, quality.embedding, saved)
    return result


@router.post("/setup/people/{label}/delete")
def delete_person(label: str, conn=Depends(get_db)):
    conn.execute("DELETE FROM enrolled_faces WHERE person_label = ?", (label,))
    conn.commit()
    return {"ok": True}


@router.post("/setup/people/{label}/rename")
def rename_person(label: str, new_name: str = Form(...), conn=Depends(get_db)):
    """Rename an enrolled person: update their enrollment AND rewrite the tag on
    every photo they're tagged in (the label IS the tag), so the change is
    consistent everywhere."""
    new_name = new_name.strip()
    if not new_name:
        raise HTTPException(status_code=400, detail="A name is required.")
    if new_name == label:
        return {"ok": True}
    existing = {r["person_label"] for r in conn.execute("SELECT DISTINCT person_label FROM enrolled_faces")}
    if label not in existing:
        raise HTTPException(status_code=404, detail="Person not found.")
    if new_name in existing:
        raise HTTPException(status_code=400, detail=f"'{new_name}' is already an enrolled person.")

    conn.execute("UPDATE enrolled_faces SET person_label = ? WHERE person_label = ?", (new_name, label))
    # Rewrite face_tags on every item that mentions the old label (LIKE narrows
    # the scan; exact list membership is checked before rewriting).
    for r in conn.execute(
        'SELECT id, face_tags FROM items WHERE face_tags LIKE ?', (f'%"{label}"%',)
    ).fetchall():
        tags = json.loads(r["face_tags"])
        if label in tags:
            tags = sorted({new_name if t == label else t for t in tags})
            conn.execute("UPDATE items SET face_tags = ? WHERE id = ?", (json.dumps(tags), r["id"]))
    conn.commit()
    return {"ok": True}


# Sentinel path for the top "This PC" level of the picker - the list of drives,
# so the user can cross to another drive or a connected/mapped device instead of
# being stuck on the drive they started on.
COMPUTER_ROOT = "::computer"


def _drive_label(root: str, letter: str) -> str:
    """'C: (Windows)' when a volume label is readable, else just 'C:'."""
    import ctypes
    try:
        buf = ctypes.create_unicode_buffer(261)
        ok = ctypes.windll.kernel32.GetVolumeInformationW(
            ctypes.c_wchar_p(root), buf, 261, None, None, None, None, 0
        )
        label = buf.value.strip() if ok else ""
    except Exception:
        label = ""
    return f"{letter}: ({label})" if label else f"{letter}:"


def _windows_drives() -> list[dict]:
    """Available drive roots (local, USB/removable, and mapped network drives).
    Uses the GetLogicalDrives bitmask, which never touches the media - so an empty
    card reader won't raise an 'insert disk' prompt the way probing the path would."""
    import ctypes
    import string
    out = []
    try:
        bitmask = ctypes.windll.kernel32.GetLogicalDrives()
    except Exception:
        return out
    for i, letter in enumerate(string.ascii_uppercase):
        if bitmask & (1 << i):
            root = f"{letter}:\\"
            out.append({"name": _drive_label(root, letter), "path": root})
    return out


@router.get("/setup/browse")
def browse(path: str = ""):
    """Read-only folder browser for the click-through picker (import / music /
    output). Lists sub-directories only; never writes.

    A special COMPUTER_ROOT level lists the machine's drives so the user can move
    across drives / connected devices; a drive root's parent points back to it."""
    is_windows = os.name == "nt"

    if path == COMPUTER_ROOT:
        if is_windows:
            return {"path": COMPUTER_ROOT, "label": "This PC", "parent": None,
                    "dirs": _windows_drives()}
        path = "/"  # POSIX has a single rooted tree; "This PC" is just /

    base = Path(path) if path else Path.home()
    try:
        base = base.resolve()
        if not base.is_dir():
            base = Path.home().resolve()
        subdirs = [p for p in base.iterdir() if p.is_dir() and not p.name.startswith(".")]
        subdirs.sort(key=lambda p: p.name.lower())
        dirs = [{"name": p.name, "path": str(p)} for p in subdirs]
    except (OSError, PermissionError):
        dirs = []

    if base.parent != base:
        parent = str(base.parent)
    else:
        # At a drive root (or POSIX '/'): step up to the drive list on Windows.
        parent = COMPUTER_ROOT if is_windows else None
    return {"path": str(base), "label": str(base), "parent": parent, "dirs": dirs}


@router.get("/setup/scan")
def scan_folder(path: str, exclude: str = ""):
    """Read-only look at a folder for the wizard's 'Choose photos' step: how many
    photos/clips it holds and the trip's date range - so we can confirm 'Portugal ·
    18–27 July · 223 photos and 14 clips' before importing, and prefill the import
    date filter. Never writes.

    The date range uses `media_capture_date` (EXIF/clip timestamp, not mtime) -
    the SAME source the import filter uses - so the prefilled range and what
    actually gets imported agree. mtime is deliberately avoided: on a synced Drive/
    OneDrive folder it is the download date, which would collapse the range."""
    folder = Path(path)
    if not folder.is_dir():
        raise HTTPException(status_code=400, detail=f"Folder not found: {path}")
    exclude_dirs = {s.strip() for s in exclude.split(",") if s.strip()} or None

    photos = clips = 0
    dates: list[date] = []
    for p in _iter_media_files(folder, exclude_dirs):
        if p.suffix.lower() in PHOTO_EXTENSIONS:
            photos += 1
        else:
            clips += 1
        d = media_capture_date(p)
        if d is not None:
            dates.append(d)

    return {
        "ok": True,
        "name": folder.name,
        "photos": photos,
        "clips": clips,
        "date_start": min(dates).isoformat() if dates else None,
        "date_end": max(dates).isoformat() if dates else None,
    }


@router.get("/setup/thumbs")
def setup_thumbs(path: str, n: int = 5):
    """A handful of photo paths spread across the chosen folder, so the wizard can
    show the user the actual contents of the folder they picked (Step 1) and use a
    few as the film-mood previews (Step 3). Read-only."""
    folder = Path(path)
    if not folder.is_dir():
        raise HTTPException(status_code=400, detail=f"Folder not found: {path}")
    photos = [p for p in _iter_media_files(folder) if p.suffix.lower() in PHOTO_EXTENSIONS]
    if not photos:
        return {"thumbs": []}
    step = max(1, len(photos) // max(1, n))
    picked = photos[::step][:n]
    return {"thumbs": [str(p) for p in picked]}


@router.get("/setup/thumb")
def setup_thumb(path: str):
    """Serve a small, EXIF-corrected JPEG thumbnail of a photo file in the chosen
    trip folder (local single-user app; the folder picker already browses the
    filesystem). Photos only."""
    p = Path(path)
    if p.suffix.lower() not in PHOTO_EXTENSIONS or not p.is_file():
        raise HTTPException(status_code=404, detail="not found")
    try:
        import io
        from PIL import Image
        from ingest.image_utils import load_corrected_rgb

        img = Image.fromarray(load_corrected_rgb(str(p)))
        img.thumbnail((420, 420))
        buf = io.BytesIO()
        img.convert("RGB").save(buf, format="JPEG", quality=82)
    except Exception:
        raise HTTPException(status_code=404, detail="unreadable")
    return Response(content=buf.getvalue(), media_type="image/jpeg")


def _parse_date_field(value: str) -> date | None:
    """Empty string -> None (no bound). Otherwise an ISO YYYY-MM-DD date."""
    value = (value or "").strip()
    if not value:
        return None
    try:
        return date.fromisoformat(value)
    except ValueError:
        raise HTTPException(status_code=400, detail=f"Invalid date: {value!r} (expected YYYY-MM-DD).")


@router.post("/setup/import")
def start_import(
    folder: str = Form(...),
    gap_threshold_minutes: float = Form(DEFAULT_GAP_THRESHOLD_MINUTES),
    exclude: str = Form(""),
    date_start: str = Form(""),
    date_end: str = Form(""),
    conn=Depends(get_db),
):
    if _get_status()["state"] == "running":
        raise HTTPException(status_code=409, detail="An import is already running.")
    folder_path = Path(folder)
    if not folder_path.is_dir():
        raise HTTPException(status_code=400, detail=f"Folder not found: {folder}")

    # Validate the cheap inputs (dates) before any DB/enrollment work, so a bad
    # range fails fast without side effects.
    start = _parse_date_field(date_start)
    end = _parse_date_field(date_end)
    if start and end and start > end:
        raise HTTPException(status_code=400, detail="Start date is after the end date.")
    date_filtered = start is not None or end is not None

    enrolled = load_enrolled(conn)
    if not enrolled:
        raise HTTPException(status_code=400, detail="Enroll at least one person first.")

    # No location lookup during import (it's on-demand per photo in review now), so
    # import needs no API key or trip country - those are checked only when the user
    # triggers a per-photo search.
    exclude_dirs = {s.strip() for s in exclude.split(",") if s.strip()} or None
    _set_status(state="running", processed=0, total=0, message="Starting…",
                date_filtered=date_filtered, skipped_out_of_range=0, included_undated=0)

    def job():
        def cb(processed, total, message):
            kw = {"message": message}
            if processed is not None:
                kw["processed"] = processed
            if total is not None:
                kw["total"] = total
            _set_status(**kw)
        try:
            counts = run_ingest(str(folder_path), enrolled, gap_threshold_minutes,
                                exclude_dirs=exclude_dirs, progress_cb=cb,
                                date_start=start, date_end=end)
            _set_status(state="done", message="Import complete.",
                        skipped_out_of_range=counts["skipped_out_of_range"],
                        included_undated=counts["included_undated"])
        except Exception as e:  # surfaced to the UI, never crashes the server
            _set_status(state="error", message=f"{type(e).__name__}: {e}")

    threading.Thread(target=job, daemon=True).start()
    return {"ok": True}


@router.get("/setup/import/status")
def import_status():
    return _get_status()


def _safe_relpath(raw: str) -> Path | None:
    """A client-supplied name/relative-path reduced to a safe relative Path:
    forward-slashed, with drive anchors, '', '.', and '..' stripped so an upload
    can never escape its destination folder. Returns None if nothing is left."""
    raw = (raw or "").replace("\\", "/").strip()
    parts = [
        p for p in PurePosixPath(raw).parts
        if p not in ("", ".", "..", "/") and not (len(p) == 2 and p[1] == ":")
    ]
    return Path(*parts) if parts else None


def _uniquify(path: Path) -> Path:
    """`foo.jpg` -> `foo_1.jpg` -> `foo_2.jpg` … so two sources with the same
    filename don't overwrite each other."""
    if not path.exists():
        return path
    stem, suffix = path.stem, path.suffix
    i = 1
    while True:
        cand = path.with_name(f"{stem}_{i}{suffix}")
        if not cand.exists():
            return cand
        i += 1


def _save_uploads(files: list[UploadFile], rel_paths: list[str], dest_dir: Path) -> int:
    """Stream uploaded photos/clips into dest_dir (preserving any relative
    sub-path), skipping non-media, uniquifying collisions. Returns how many were
    saved."""
    dest_dir.mkdir(parents=True, exist_ok=True)
    saved = 0
    for i, up in enumerate(files):
        raw = (rel_paths[i] if i < len(rel_paths) else "") or (up.filename or "")
        safe = _safe_relpath(raw)
        if safe is None or classify_media(safe) is None:
            continue  # empty name, or not a photo/clip we handle
        target = _uniquify(dest_dir / safe)
        target.parent.mkdir(parents=True, exist_ok=True)
        with target.open("wb") as out:
            shutil.copyfileobj(up.file, out)
        saved += 1
    return saved


@router.post("/setup/upload-media")
def upload_media(files: list[UploadFile] = File(...), paths: list[str] = Form(default=[])):
    """Drag-and-drop / file-upload import: save the dropped photos/clips into the
    current project's own media store, then hand the resulting folder back so the
    normal folder flow takes over (scan -> date prefill -> /setup/import). Files
    are COPIED and kept, because import stores their path and serving/render
    re-read it later. See docs/specs/drag-and-drop-import.md."""
    if _get_status()["state"] == "running":
        raise HTTPException(status_code=409, detail="An import is already running.")
    if not files:
        raise HTTPException(status_code=400, detail="No files were provided.")

    # Per-project store so a project's uploaded media travels with it (and can be
    # cleaned up with the project); flat fallback if there's no current project.
    pid = project_store.current_id()
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    dest = (project_store.PROJECTS_DIR / pid / "media" / ts) if pid else (DATA_DIR / "imported_media" / ts)

    saved = _save_uploads(files, paths, dest)
    if not saved:
        try:
            dest.rmdir()
        except OSError:
            pass
        raise HTTPException(status_code=400, detail="None of the dropped items are photos or clips.")
    return {"ok": True, "path": str(dest), "count": saved}


@router.post("/setup/new-trip")
def new_trip():
    """Archive the current trip aside and start fresh, keeping enrolled people.

    The catalog is cleared with SQL DELETEs on the live connection - NOT by moving
    the .db file. Moving an open-able SQLite file fails on Windows with WinError 32
    whenever any handle is open (e.g. a lingering request or the generate thread),
    which silently left the old trip's photos in the review pane after the "new
    trip" + re-import. SQL deletes always take effect, and a SQLite backup gives us
    the archive snapshot without touching the file on disk."""
    if _get_status()["state"] == "running":
        raise HTTPException(status_code=409, detail="Wait for the current import to finish.")

    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    archive = DATA_DIR / "archive" / ts
    archive.mkdir(parents=True, exist_ok=True)

    conn = get_connection(DEFAULT_DB_PATH)
    try:
        # Snapshot the current catalog for the archive (backup API works even while
        # the DB is in use - no file move, so open handles can't block it).
        dest = get_connection(archive / "catalog.db")
        try:
            conn.backup(dest)
        finally:
            dest.close()
        # Clear the trip but KEEP enrolled_faces so people don't re-enroll. Delete
        # items before clusters (items.cluster_id references clusters).
        conn.execute("DELETE FROM items")
        conn.execute("DELETE FROM clusters")
        conn.execute("DELETE FROM skipped_files")
        conn.execute("DELETE FROM day_captions")
        conn.execute("UPDATE trip_intro SET intro_text = '', outro_text = '' WHERE id = 1")
        conn.commit()
    finally:
        conn.close()

    # Move the rendered outputs aside. Best-effort: a file still open (e.g. a video
    # being previewed) must not abort the reset - the catalog is already cleared.
    for name in ("output", "blurred"):
        src = DATA_DIR / name
        if src.exists():
            try:
                shutil.move(str(src), str(archive / name))
            except OSError:
                pass

    _set_status(state="idle", processed=0, total=0, message="")
    return {"ok": True, "archived_to": str(archive)}


@router.post("/setup/itinerary")
async def upload_itinerary(file: UploadFile = File(...), conn=Depends(get_db)):
    tmp = DATA_DIR / "_itinerary_upload.txt"
    tmp.parent.mkdir(parents=True, exist_ok=True)
    tmp.write_bytes(await file.read())
    try:
        itinerary = parse_itinerary(str(tmp))
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"Couldn't parse itinerary: {e}")
    finally:
        tmp.unlink(missing_ok=True)
    for trip_day, day in itinerary.items():
        conn.execute(
            "INSERT OR IGNORE INTO day_captions (trip_day, caption_text) VALUES (?, ?)",
            (trip_day, day.short_info),
        )
    conn.commit()
    # The itinerary's day keys are ISO dates; their span is the trip's date range,
    # which the wizard uses to prefill the import date filter (overriding the
    # scan-detected range).
    days = sorted(itinerary.keys())
    return {"ok": True, "days": len(itinerary), "date_start": days[0], "date_end": days[-1]}


@router.post("/setup/settings")
def save_settings(
    mood_preset: str | None = Form(None),
    video_style: str | None = Form(None),
    motion_style: str | None = Form(None),
    photo_duration_seconds: str | None = Form(None),
    auto_place_id: str | None = Form(None),
    trip_country: str | None = Form(None),
    llm_provider: str | None = Form(None),
    llm_model: str | None = Form(None),
    llm_api_key: str | None = Form(None),
    llm_base_url: str | None = Form(None),
    music_dir: str | None = Form(None),
    output_dir: str | None = Form(None),
    output_basename: str | None = Form(None),
):
    """Partial save: only the fields actually submitted are updated. The wizard
    splits settings across steps (the Film-mood step posts mood + look; the
    Advanced drawer posts place-finding + folders), so each form must leave the
    others untouched. A field sent as "" is an intentional clear; a field not
    sent at all (None) is left as-is."""
    if mood_preset is not None and mood_preset.strip():
        config.set_setting("mood_preset", mood_preset.strip())
    if video_style is not None:
        config.set_setting("video_style", video_style)
    if motion_style is not None:
        config.set_setting("motion_style", motion_style)
    if photo_duration_seconds is not None:
        try:
            secs = float(photo_duration_seconds)
            config.set_setting("photo_duration_seconds", str(secs if secs > 0 else 4))
        except (TypeError, ValueError):
            pass
    if auto_place_id is not None:
        config.set_setting("auto_place_id", auto_place_id)
    if trip_country is not None:
        config.set_setting("trip_country", trip_country.strip())
    if llm_provider is not None:
        config.set_setting("llm_provider", llm_provider)
    if llm_base_url is not None:
        config.set_setting("llm_base_url", llm_base_url.strip())
    if llm_model is not None and llm_model.strip():
        config.set_setting("llm_model", llm_model.strip())
    if llm_api_key is not None and llm_api_key.strip():  # only overwrite when a new one is typed
        secret_store.set_api_key(llm_api_key.strip())
    if music_dir is not None and music_dir.strip():
        config.set_setting("music_dir", music_dir.strip())
    if output_dir is not None and output_dir.strip():
        config.set_setting("output_dir", output_dir.strip())
    if output_basename is not None and output_basename.strip():
        config.set_setting("output_basename", output_basename.strip())
    return {"ok": True}


@router.post("/setup/test-llm")
def test_llm_route():
    ok, message = test_llm()
    return {"ok": ok, "message": message}


# --- music (upload / preview / reorder / remove) ---
def _music_dir() -> Path:
    d = Path(config.get_setting("music_dir"))
    d.mkdir(parents=True, exist_ok=True)
    return d


@router.get("/media/music/{name}")
def serve_music(name: str):
    if Path(name).name != name:  # no path traversal
        raise HTTPException(status_code=404, detail="not found")
    path = _music_dir() / name
    if not path.is_file():
        raise HTTPException(status_code=404, detail="not found")
    return FileResponse(str(path))


@router.post("/setup/music")
async def upload_music(tracks: list[UploadFile] = File(...)):
    dst = _music_dir()
    order = list(config.get_setting("music_order", []) or [])
    saved = 0
    for up in tracks:
        if not up.filename or not up.filename.lower().endswith(".mp3"):
            continue
        name = Path(up.filename).name
        (dst / name).write_bytes(await up.read())
        if name not in order:
            order.append(name)
        saved += 1
    if saved == 0:
        raise HTTPException(status_code=400, detail="Upload at least one .mp3 file.")
    config.set_setting("music_order", order)
    return {"ok": True, "added": saved}


@router.post("/setup/music/{name}/delete")
def delete_music(name: str):
    if Path(name).name != name:
        raise HTTPException(status_code=404, detail="not found")
    (_music_dir() / name).unlink(missing_ok=True)
    order = [n for n in (config.get_setting("music_order", []) or []) if n != name]
    config.set_setting("music_order", order)
    return {"ok": True}


@router.post("/setup/music/order")
def reorder_music(order: list[str] = Form(...)):
    config.set_setting("music_order", order)
    return {"ok": True}
