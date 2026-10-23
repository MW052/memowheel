"""Find-in-library: the standalone UI over the shared face_index engine.

Designate one or more photo-library folders, index them (background job with a
progress bar), then search for an enrolled person or a group and get a ranked
grid of matching photos and clips - with the clip timestamp where the person
appears. Results can be copied or moved to a folder. The matching/indexing all
live in `face_index.py`; this module is just the HTTP surface.
"""

import io
import shutil
import threading
import time
from pathlib import Path

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import Response

import face_index
import people
from db.schema import get_connection
from review_app.db import get_db
from review_app.templating import templates

router = APIRouter()

# Background re-index job (single-user local app: one at a time, in-memory status).
_reindex_lock = threading.Lock()
_reindex_status = {"state": "idle", "done": 0, "total": 0, "message": "",
                   "summary": None, "started": None, "elapsed": 0.0}


def _set(**kw):
    with _reindex_lock:
        _reindex_status.update(kw)


def _get() -> dict:
    with _reindex_lock:
        s = dict(_reindex_status)
    # Live elapsed while running; the final value is frozen when the job ends.
    if s["state"] == "running" and s.get("started"):
        s["elapsed"] = time.time() - s["started"]
    return s


def _targets(conn) -> list[dict]:
    """Searchable targets: enrolled people and groups (things that carry
    embeddings), for the search box's type-ahead."""
    ppl = [{"name": p["label"], "kind": "person"} for p in people.list_people(conn)]
    grp = [{"name": g["name"], "kind": "group"} for g in people.list_groups(conn)]
    return ppl + grp


@router.get("/gallery")
def gallery_page(request: Request, conn=Depends(get_db)):
    return templates.TemplateResponse(request, "gallery.html", {
        "libraries": face_index.library_dirs(),
        "status": face_index.index_status(),
        "targets": _targets(conn),
        "reindex": _get(),
        "tiers": ["strict", "balanced", "loose"],
    })


# --- library folders ---
@router.post("/gallery/libraries/add")
def add_library(folder: str = Form(...)):
    folder = folder.strip()
    if not Path(folder).is_dir():
        raise HTTPException(status_code=400, detail=f"Folder not found: {folder}")
    face_index.add_library_dir(folder)
    return {"ok": True}


@router.post("/gallery/libraries/remove")
def remove_library(folder: str = Form(...)):
    face_index.remove_library_dir(folder)
    return {"ok": True}


# --- indexing ---
@router.post("/gallery/reindex")
def reindex():
    if _get()["state"] == "running":
        raise HTTPException(status_code=409, detail="Indexing is already running.")
    if not face_index.library_dirs():
        raise HTTPException(status_code=400, detail="Add a library folder first.")
    start = time.time()
    _set(state="running", done=0, total=0, message="Starting…", summary=None,
         started=start, elapsed=0.0)

    def job():
        def cb(done, total, message):
            _set(done=done, total=total, message=message)
        try:
            summary = face_index.refresh(progress_cb=cb)
            _set(state="done", message="Indexing complete.", summary=summary,
                 elapsed=time.time() - start)
        except Exception as e:  # surfaced to the UI, never crashes the server
            _set(state="error", message=f"{type(e).__name__}: {e}",
                 elapsed=time.time() - start)

    threading.Thread(target=job, daemon=True).start()
    return {"ok": True}


@router.get("/gallery/reindex/status")
def reindex_status():
    return _get()


# --- search ---
@router.get("/gallery/search")
def gallery_search(request: Request, q: str = "", tier: str = "balanced", conn=Depends(get_db)):
    q = (q or "").strip()
    tier = tier if tier in ("strict", "balanced", "loose") else "balanced"
    result = None
    if q:
        people_names = {p["label"] for p in people.list_people(conn)}
        group_names = {g["name"] for g in people.list_groups(conn)}
        # Prefer an exact person, then an exact group (case-insensitive).
        ql = q.casefold()
        person = next((n for n in people_names if n.casefold() == ql), None)
        group = next((n for n in group_names if n.casefold() == ql), None)
        if person:
            hits, kind, matched = face_index.find_person(person, tier), "person", person
        elif group:
            hits, kind, matched = face_index.find_group(group, tier), "group", group
        else:
            hits, kind, matched = [], None, None
        # Keep photos and clips apart (hits arrive already ranked best-match
        # first), and within each keep strong vs maybe apart too.
        def split(kind_):
            ks = [h for h in hits if h["kind"] == kind_]
            return {"confident": [h for h in ks if h["tier"] == "confident"],
                    "maybe": [h for h in ks if h["tier"] == "maybe"]}
        result = {"kind": kind, "matched": matched, "total": len(hits),
                  "photos": split("photo"), "clips": split("clip")}
    return templates.TemplateResponse(request, "gallery_results.html", {
        "q": q,
        "tier": tier,
        "tiers": ["strict", "balanced", "loose"],
        "targets": _targets(conn),
        "result": result,
        "status": face_index.index_status(),
    })


def _is_indexed(path: str) -> bool:
    """Only serve files that are actually in the index - never an arbitrary path."""
    face_index.init_index_db()
    c = get_connection(face_index.INDEX_DB)
    try:
        return c.execute("SELECT 1 FROM media_index WHERE path = ?", (path,)).fetchone() is not None
    finally:
        c.close()


@router.get("/gallery/media")
def gallery_media(path: str, t: int = -1, full: int = 0):
    """A result image: an EXIF-corrected JPEG for a photo, or the frame at `t` ms
    for a clip. `full=1` serves a large (lightbox) version; otherwise a grid
    thumbnail. Restricted to indexed files."""
    if not _is_indexed(path):
        raise HTTPException(status_code=404, detail="not indexed")
    try:
        import numpy as np
        from PIL import Image

        if t >= 0:  # a clip frame
            import cv2
            cap = cv2.VideoCapture(path)
            try:
                cap.set(cv2.CAP_PROP_POS_MSEC, float(t))
                ok, frame = cap.read()
            finally:
                cap.release()
            if not ok:
                raise RuntimeError("frame read failed")
            rgb = frame[:, :, ::-1]
        else:  # a photo
            from ingest.image_utils import load_corrected_rgb
            rgb = load_corrected_rgb(path)
        img = Image.fromarray(np.ascontiguousarray(rgb))
        img.thumbnail((1600, 1600) if full else (640, 640))
        buf = io.BytesIO()
        img.convert("RGB").save(buf, format="JPEG", quality=88 if full else 84)
    except Exception:
        raise HTTPException(status_code=404, detail="unreadable")
    return Response(content=buf.getvalue(), media_type="image/jpeg")


# --- export (copy / move matches to a folder) ---
@router.post("/gallery/export")
def gallery_export(dest: str = Form(...), mode: str = Form("copy"), paths: list[str] = Form(default=[])):
    mode = "move" if mode == "move" else "copy"
    dest_dir = Path(dest.strip())
    if not dest.strip():
        raise HTTPException(status_code=400, detail="Choose a destination folder.")
    dest_dir.mkdir(parents=True, exist_ok=True)
    done = 0
    for p in paths:
        if not _is_indexed(p) or not Path(p).is_file():
            continue
        target = dest_dir / Path(p).name
        i = 1
        while target.exists():  # don't overwrite same-named files
            target = dest_dir / f"{Path(p).stem}_{i}{Path(p).suffix}"
            i += 1
        try:
            if mode == "move":
                shutil.move(p, str(target))
            else:
                shutil.copy2(p, str(target))
            done += 1
        except OSError:
            continue
    return {"ok": True, "mode": mode, "count": done, "dest": str(dest_dir)}


# --- "this isn't them": remove a result from a target's future matches ---
@router.post("/gallery/exclude")
def gallery_exclude(target: str = Form(...), path: str = Form(...)):
    """Hide this media from `target`'s (a person or group name) results, now and
    on future searches. If the target is a group, exclude it from each member too,
    so it won't resurface via a member's own search."""
    target = target.strip()
    if not target or not path:
        raise HTTPException(status_code=400, detail="target and path are required.")
    face_index.exclude_match(target, path)
    for member in face_index._group_member_labels(target):  # empty for a person
        face_index.exclude_match(member, path)
    return {"ok": True}
