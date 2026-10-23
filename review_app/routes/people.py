"""People & Groups page, and name/group photo search.

The reusable domain logic lives in the web-independent `people.py` (so future
makers can import it); this module is just the HTTP surface:

- GET  /people                       -> the People & Groups management page
- POST /people/groups                -> create a group
- POST /people/groups/{id}/rename    -> rename a group
- POST /people/groups/{id}/delete    -> delete a group
- POST /people/groups/{id}/members   -> add / remove a person (action=add|remove)
- GET  /people/search?q=&scope=      -> results grid (scope: current|all)
- GET  /people/search/media          -> a result thumbnail, scoped to its project
"""

import io

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse, Response

import face_index
import paths
import people
import project_store
from db.schema import DEFAULT_DB_PATH, get_connection
from review_app.db import get_db
from review_app.templating import templates

router = APIRouter()


@router.get("/people/home", response_class=HTMLResponse)
def people_home(request: Request, conn=Depends(get_db)):
    """Top-level People home: the shared people domain (identities + library
    search), a peer to the makers - not part of any one of them."""
    return templates.TemplateResponse(request, "people_home.html", {
        "people_count": len(people.list_people(conn)),
        "group_count": len(people.list_groups(conn)),
        "index": face_index.index_status(),
    })


@router.get("/people", response_class=HTMLResponse)
def people_page(request: Request, conn=Depends(get_db)):
    return templates.TemplateResponse(request, "people.html", {
        "people": people.list_people(conn),
        "groups": people.list_groups(conn),
        "name_options": people.search_suggestions(conn),
        "orphans": people.orphan_labels_all(conn),
    })


@router.post("/people/resync")
def resync_tags(old: list[str] = Form([]), new: list[str] = Form([]), conn=Depends(get_db)):
    """Remap drifted photo tags to current people across every trip. Takes
    parallel `old` (orphan label) / `new` (chosen enrolled person, or "" to
    leave) arrays; applies only the rows the user mapped to a valid person."""
    valid = people.person_labels(conn)
    mapping = {}
    for o, n in zip(old, new):
        n = (n or "").strip()
        if n and n != o and n in valid:
            mapping[o] = n
    changed = people.relabel_in_catalog(conn, mapping) + people.relabel_other_projects(mapping)
    return {"ok": True, "mapped": len(mapping), "changed": changed}


# --- group management (thin wrappers over people.py; ValueError -> 400) ---
def _ok(fn, *a):
    try:
        result = fn(*a)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    return {"ok": True, "id": result} if isinstance(result, int) else {"ok": True}


@router.post("/people/groups")
def create_group(name: str = Form(...), conn=Depends(get_db)):
    return _ok(people.create_group, conn, name)


@router.post("/people/groups/{group_id}/rename")
def rename_group(group_id: int, name: str = Form(...), conn=Depends(get_db)):
    return _ok(people.rename_group, conn, group_id, name)


@router.post("/people/groups/{group_id}/delete")
def delete_group(group_id: int, conn=Depends(get_db)):
    return _ok(people.delete_group, conn, group_id)


@router.post("/people/groups/{group_id}/members")
def edit_members(group_id: int, label: str = Form(...), action: str = Form(...), conn=Depends(get_db)):
    if action == "add":
        return _ok(people.add_member, conn, group_id, label)
    if action == "remove":
        return _ok(people.remove_member, conn, group_id, label)
    raise HTTPException(status_code=400, detail="action must be 'add' or 'remove'")


# --- search ---
@router.get("/people/search", response_class=HTMLResponse)
def search_page(request: Request, q: str = "", scope: str = "current", conn=Depends(get_db)):
    scope = "all" if scope == "all" else "current"
    results = people.search(scope, q) if q.strip() else None
    return templates.TemplateResponse(request, "people_search.html", {
        "q": q,
        "scope": scope,
        "results": results,
        "name_options": people.search_suggestions(conn),
    })


def _catalog_for_project(project_id: str | None):
    """The catalog a result thumbnail should be read from. The current project's
    authoritative copy is the live catalog (it may hold unsaved edits); other
    projects are read from their own file."""
    if not project_id or project_id == project_store.current_id():
        return DEFAULT_DB_PATH
    return project_store._project_db(project_id)


@router.get("/people/search/media")
def search_media(item_id: int, project: str = ""):
    """Serve one result's photo/clip, scoped to the project it came from. Item ids
    collide across projects, so /media/{id} (live DB only) can't serve them; this
    route resolves the right catalog first."""
    db_path = _catalog_for_project(project or None)
    if not db_path.is_file():
        raise HTTPException(status_code=404, detail="project not found")
    conn = get_connection(db_path)
    try:
        row = conn.execute(
            "SELECT path, item_type, rotation, crop FROM items WHERE id = ?", (item_id,)
        ).fetchone()
    finally:
        conn.close()
    if row is None:
        raise HTTPException(status_code=404, detail="item not found")

    if row["item_type"] != "photo":
        # Clip: stream the file; the tile's <video preload="metadata"> shows a frame.
        return FileResponse(row["path"])

    # Photo: serve an EXIF-corrected, rotated/cropped thumbnail (same correction
    # the review grid uses, downscaled - a result grid never needs full-res).
    try:
        import numpy as np
        from PIL import Image
        from ingest.image_utils import load_corrected_rgb, apply_crop

        rgb = load_corrected_rgb(row["path"])
        rotation = (row["rotation"] or 0) % 360
        if rotation:
            rgb = np.rot90(rgb, -(rotation // 90) % 4)
        rgb = apply_crop(rgb, row["crop"])
        img = Image.fromarray(rgb)
        img.thumbnail((640, 640))
        buf = io.BytesIO()
        img.convert("RGB").save(buf, format="JPEG", quality=84)
    except Exception:
        raise HTTPException(status_code=404, detail="unreadable")
    return Response(content=buf.getvalue(), media_type="image/jpeg")
