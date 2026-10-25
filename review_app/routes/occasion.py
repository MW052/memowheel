"""Occasion Video - the maker's HTTP surface.

Domain logic lives in the web-independent `occasion.py`; face search in
`face_index.py`; identities in `people.py`. This module is just the pages:

- GET  /occasion       -> chooser: your occasions + "new", with prerequisites
- GET  /occasion/new   -> the new-occasion form (type / honoree / framing)
- POST /occasion/new   -> gather the honoree's photos, create+populate the
                          project, land in the review workspace
"""

import io

from fastapi import APIRouter, Depends, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import HTMLResponse, RedirectResponse
from PIL import Image, ImageOps

import config
import face_index
import occasion as occ
import people
import project_store
from db.schema import DEFAULT_DB_PATH, get_connection
from review_app.db import get_db
from review_app.templating import templates

router = APIRouter()

_TIERS = ("strict", "balanced", "loose")
_COVER_MAX_EDGE = 1920


def _process_cover(upload: UploadFile | None) -> bytes | None:
    """Validate + normalize an uploaded cover photo: EXIF-corrected, downscaled to
    a sane size, re-encoded JPEG. Returns None if there's no file or it isn't a
    readable image (so a bad upload is simply ignored, never fatal)."""
    if upload is None or not (upload.filename or "").strip():
        return None
    try:
        raw = upload.file.read()
        img = Image.open(io.BytesIO(raw))
        img = ImageOps.exif_transpose(img).convert("RGB")
        img.thumbnail((_COVER_MAX_EDGE, _COVER_MAX_EDGE), Image.LANCZOS)
        buf = io.BytesIO()
        img.save(buf, format="JPEG", quality=88)
        return buf.getvalue()
    except Exception:
        return None


def _targets(conn) -> list[dict]:
    """Enrolled people + groups, for the honoree type-ahead."""
    ppl = [{"name": p["label"], "kind": "person"} for p in people.list_people(conn)]
    grp = [{"name": g["name"], "kind": "group"} for g in people.list_groups(conn)]
    return ppl + grp


@router.get("/occasion", response_class=HTMLResponse)
def occasion_home(request: Request, conn=Depends(get_db)):
    projects = [p for p in project_store.list_projects()
                if project_store.project_kind(p["id"]) == "occasion"]
    return templates.TemplateResponse(request, "occasion_home.html", {
        "projects": projects,
        "current": project_store.current(),
        "people_count": len(people.list_people(conn)),
        "index": face_index.index_status(),
    })


@router.get("/occasion/new", response_class=HTMLResponse)
def occasion_new_form(request: Request, conn=Depends(get_db)):
    return templates.TemplateResponse(request, "occasion_new.html", {
        "targets": _targets(conn),
        "types": occ.OCCASION_TYPES,
        "styles": occ.OCCASION_STYLES,   # {type: [style_key, ...]}
        "swatches": occ.OCCASION_STYLE_SWATCHES,
        "tiers": _TIERS,
        "people_count": len(people.list_people(conn)),
        "index": face_index.index_status(),
        "libraries": face_index.library_dirs(),
        "decor_options": occ.DECOR_OPTIONS,
        "music_tracks": _music_names(),
    })


def _music_names() -> list[str]:
    return [p.name for p in config.ordered_music_files(config.get_setting("music_dir"))]


@router.get("/occasion/music", response_class=HTMLResponse)
def occasion_music_page(request: Request):
    """A standalone soundtrack page (reached from the menu), like Trip Video's
    music step — add / preview / remove / reorder the shared tracks."""
    return templates.TemplateResponse(request, "occasion_music.html", {
        "music_tracks": _music_names(),
    })


@router.get("/occasion/music/list")
def occasion_music_list():
    """Current soundtrack tracks (ordered) — lets a music UI refresh in place
    after an upload/remove/reorder without reloading."""
    return {"tracks": _music_names()}


@router.post("/occasion/new")
def occasion_create(
    occasion_type: str = Form("birthday"),
    honoree: str = Form(...),
    tier: str = Form("balanced"),
    headline: str = Form(""),
    dedication: str = Form(""),
    closing: str = Form(""),
    theme: str = Form(""),
    decor: str = Form("auto"),
    intro_cover: UploadFile | None = File(None),
    outro_cover: UploadFile | None = File(None),
):
    # Normalize covers up front (reads the upload streams before anything else).
    intro_cover_b = _process_cover(intro_cover)
    outro_cover_b = _process_cover(outro_cover)
    honoree = (honoree or "").strip()
    if not honoree:
        raise HTTPException(status_code=400, detail="Choose who the occasion is for.")
    tier = tier if tier in _TIERS else "balanced"
    # set_facet validates the theme against the chosen type's styles.

    # Resolve the honoree to an enrolled person or a group (case-insensitive),
    # reading the shared people domain from the currently-live catalog BEFORE we
    # switch projects. People are identical across projects, so this is stable.
    live = get_connection(DEFAULT_DB_PATH)
    try:
        people_names = {p["label"] for p in people.list_people(live)}
        group_names = {g["name"] for g in people.list_groups(live)}
    finally:
        live.close()
    hl = honoree.casefold()
    person = next((n for n in people_names if n.casefold() == hl), None)
    group = None if person else next((n for n in group_names if n.casefold() == hl), None)
    if not person and not group:
        raise HTTPException(
            status_code=400,
            detail=f"'{honoree}' is not an enrolled person or group. Enroll them first.",
        )

    # Create the occasion project (people are kept; items start empty), then
    # gather the honoree's matches from the global index and populate it.
    name = headline.strip() or honoree
    project_store.create_project(name, kind="occasion")
    conn = get_connection(DEFAULT_DB_PATH)
    try:
        occ.set_facet(conn, occasion_type=occasion_type, honoree=honoree,
                      headline=headline.strip(), dedication=dedication.strip(),
                      closing=closing.strip(), theme=theme, decor=decor)
        if intro_cover_b:
            occ.set_cover(conn, "intro", intro_cover_b)
        if outro_cover_b:
            occ.set_cover(conn, "outro", outro_cover_b)
        # Framing text lives in trip_intro (shared by both makers), so the review
        # "Opening / Closing" fields edit the very text the renderer uses. The
        # opening slide is the headline then the dedication.
        intro_text = "\n".join(t for t in (headline.strip(), dedication.strip()) if t)
        conn.execute(
            "INSERT INTO trip_intro (id, intro_text, outro_text) VALUES (1, ?, ?) "
            "ON CONFLICT(id) DO UPDATE SET intro_text=excluded.intro_text, "
            "outro_text=excluded.outro_text",
            (intro_text, closing.strip()),
        )
        conn.commit()
        if person:
            hits = face_index.find_person(person, tier)
            tag_label = person
        else:
            hits = face_index.find_group(group, tier)
            tag_label = None
        occ.build_from_hits(conn, hits, tag_label=tag_label)
    finally:
        conn.close()
    # Land on the triage grid to confirm/exclude the matches before review.
    return RedirectResponse("/occasion/triage", status_code=303)


@router.get("/occasion/triage", response_class=HTMLResponse)
def occasion_triage(request: Request, conn=Depends(get_db)):
    """Confirm the gathered matches before diving into review: strong matches are
    in the film, 'maybe' ones are set aside to opt in; either can be toggled, and
    a wrong match can be removed (and kept out of this person's future results)."""
    facet = occ.get_facet(conn)
    rows = conn.execute(
        "SELECT id, item_type, include_in_video, timestamp FROM items "
        "ORDER BY include_in_video DESC, sort_order, timestamp"
    ).fetchall()
    strong = [dict(r) for r in rows if r["include_in_video"]]
    maybe = [dict(r) for r in rows if not r["include_in_video"]]
    return templates.TemplateResponse(request, "occasion_triage.html", {
        "honoree": facet.get("honoree", ""),
        "strong": strong,
        "maybe": maybe,
        "total": len(rows),
    })


@router.post("/occasion/triage/remove")
def occasion_triage_remove(item_id: int = Form(...), conn=Depends(get_db)):
    """Remove a wrong match from this occasion and from the honoree's future
    gather results (a per-target face-index exclusion)."""
    row = conn.execute("SELECT path FROM items WHERE id = ?", (item_id,)).fetchone()
    if row is None:
        raise HTTPException(status_code=404, detail="item not found")
    honoree = (occ.get_facet(conn).get("honoree") or "").strip()
    if honoree:
        try:
            face_index.exclude_match(honoree, row["path"])
            for member in face_index._group_member_labels(honoree):
                face_index.exclude_match(member, row["path"])
        except Exception:
            pass  # exclusion is best-effort; removal from the occasion still happens
    conn.execute("DELETE FROM items WHERE id = ?", (item_id,))
    conn.commit()
    return {"ok": True}
