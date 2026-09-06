"""Main review screen: thumbnail grid by cluster/place, include/exclude,
drag-to-reorder, gap-threshold re-cluster, Generate trigger (Premise 19)."""

import json
import threading
import time
from datetime import date, datetime, timedelta
from pathlib import Path

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse, RedirectResponse, Response

import config
import project_store
from db.schema import get_connection, DEFAULT_DB_PATH
from review_app.db import get_db
from review_app.templating import templates
from ingest.pipeline import ingest_phase2, add_items_from_path, classify_media
from ingest.faces import load_enrolled
from ingest.clustering import DEFAULT_GAP_THRESHOLD_MINUTES
from cloud.resolve_places import (
    blur_and_send,
    find_location_for_item,
)
from cloud.place_id import PlaceIdApiError

router = APIRouter()

# Video generation runs in a background thread (it's a multi-minute render); the
# review page polls /generate/status for the progress bar. Single-user local app,
# so a module-level status guarded by a lock is enough.
_gen_lock = threading.Lock()
_gen_status = {"state": "idle", "done": 0, "total": 0, "message": "", "output": "", "output_name": "", "mode": "", "elapsed": ""}


def _gen_set(**kw):
    with _gen_lock:
        _gen_status.update(kw)


def _gen_get() -> dict:
    with _gen_lock:
        return dict(_gen_status)


def _fmt_dur(secs: float) -> str:
    s = int(round(secs))
    return f"{s} sec" if s < 60 else f"{s // 60}:{s % 60:02d}"


def _day_labels(trip_day: str) -> tuple[str, str]:
    """(short, long) date labels for a YYYY-MM-DD trip_day, e.g. ('19 Jul',
    '19 July 2023'). Falls back to the raw value if it isn't a date."""
    try:
        d = datetime.strptime(trip_day, "%Y-%m-%d")
    except (TypeError, ValueError):
        return trip_day, trip_day
    return f"{d.day} {d.strftime('%b')}", f"{d.day} {d.strftime('%B %Y')}"


@router.get("/trip", response_class=HTMLResponse)
def trip_entry(request: Request):
    """Front door for the Trip Video maker. First-ever run (no projects) goes
    straight to the wizard; otherwise show the chooser: continue the last
    unfinished project, open one from the list, or start a new one."""
    project_store.ensure_current()  # adopt an existing catalog as the first project
    projects = project_store.list_projects()
    if not projects:
        return RedirectResponse("/setup", status_code=303)
    return templates.TemplateResponse(request, "trip_chooser.html", {
        "projects": projects,
        "archived": project_store.list_archived(),
        "current": project_store.current(),
        "last_unfinished": project_store.last_unfinished(),
    })


@router.post("/projects/new")
def projects_new(name: str = Form("")):
    project_store.create_project(name)
    return RedirectResponse("/setup", status_code=303)


@router.post("/projects/{pid}/open")
def projects_open(pid: str):
    if _gen_get()["state"] == "running":
        raise HTTPException(status_code=409, detail="Wait for the current film to finish rendering.")
    if not project_store.load_project(pid):
        raise HTTPException(status_code=404, detail="Project not found.")
    dest = "/review"
    proj = project_store.get_project(pid)
    if proj and proj.get("video"):
        dest = "/finish"  # finished projects open on their film
    return RedirectResponse(dest, status_code=303)


@router.post("/projects/restore-archive/{ts}")
def projects_restore_archive(ts: str):
    if _gen_get()["state"] == "running":
        raise HTTPException(status_code=409, detail="Wait for the current film to finish rendering.")
    pid = project_store.restore_archive(ts)
    if not pid:
        raise HTTPException(status_code=404, detail="archive not found")
    return RedirectResponse("/review", status_code=303)


@router.post("/projects/{pid}/rename")
def projects_rename(pid: str, name: str = Form(...)):
    project_store.rename_project(pid, name)
    return {"ok": True}


@router.post("/projects/{pid}/delete")
def projects_delete(pid: str):
    project_store.delete_project(pid)
    return {"ok": True}


@router.get("/review", response_class=HTMLResponse)
def review_page(request: Request, view: str = "", day: str = "", conn=Depends(get_db)):
    """The Story workspace: a left rail navigates Days + triage (Needs Attention /
    Unassigned); the main pane shows the selected day's moments as cover tiles.
    Opening a tile drills into the moment editor (loaded via the fragment route)."""
    clusters = conn.execute(
        "SELECT id, trip_day, place_name FROM clusters ORDER BY trip_day"
    ).fetchall()

    # Minimal item fields for tile summaries (cover / count / duration) - the full
    # per-photo editor is loaded on demand via /review/clusters/{id}/fragment.
    rows = conn.execute(
        "SELECT id, cluster_id, item_type, include_in_video, is_representative, "
        "rotation, crop, clip_start, clip_end, sort_order, timestamp "
        "FROM items WHERE cluster_id IS NOT NULL"
    ).fetchall()
    try:
        photo_seconds = float(config.get_setting("photo_duration_seconds", 4) or 4)
    except (TypeError, ValueError):
        photo_seconds = 4.0
    items_by_cluster: dict[int, list] = {}
    for r in rows:
        items_by_cluster.setdefault(r["cluster_id"], []).append(dict(r))

    def summarize(c) -> dict:
        its = sorted(
            items_by_cluster.get(c["id"], []),
            key=lambda r: (r["sort_order"] if r["sort_order"] is not None else 1 << 30, r["timestamp"] or ""),
        )
        cover = next((i for i in its if i["is_representative"]), its[0] if its else None)
        dur = 0.0
        for i in its:
            if not i["include_in_video"]:
                continue
            if i["item_type"] == "photo":
                dur += photo_seconds
            else:  # clip: trimmed length if known, else a nominal estimate
                s, e = i["clip_start"] or 0, i["clip_end"]
                dur += (e - s) if (e and e > s) else (e if e else 6.0)
        included = sum(1 for i in its if i["include_in_video"])
        return {
            "id": c["id"], "title": c["place_name"], "needs": c["place_name"] is None,
            "count": len(its), "included": included, "duration": _fmt_dur(dur),
            "cover": ({"id": cover["id"], "type": cover["item_type"],
                       "rotation": cover["rotation"], "crop": cover["crop"]} if cover else None),
        }

    captions = {r["trip_day"]: r["caption_text"]
                for r in conn.execute("SELECT trip_day, caption_text FROM day_captions").fetchall()}
    clusters_by_day: dict[str, list] = {}
    for c in clusters:
        clusters_by_day.setdefault(c["trip_day"], []).append(c)
    days = []
    for idx, td in enumerate(sorted(clusters_by_day), start=1):
        short, long = _day_labels(td)
        days.append({"trip_day": td, "index": idx, "count": len(clusters_by_day[td]),
                     "short": short, "long": long, "caption": captions.get(td, "")})

    overview = {
        "all_moments": len(clusters),
        "needs_attention": sum(1 for c in clusters if c["place_name"] is None),
        "unassigned": conn.execute("SELECT COUNT(*) AS n FROM items WHERE cluster_id IS NULL").fetchone()["n"],
    }

    if view not in ("all", "needs", "unassigned"):
        view = ""
    if not view and not day and days:
        day = days[0]["trip_day"]
    sel_day = next((d for d in days if d["trip_day"] == day), None)
    if not view and sel_day is None and days:
        sel_day, day = days[0], days[0]["trip_day"]

    tiles, day_sections, orphan_items = [], [], []
    if view == "all":
        day_sections = [{"day": d, "tiles": [summarize(c) for c in clusters_by_day[d["trip_day"]]]} for d in days]
    elif view == "needs":
        tiles = [summarize(c) for c in clusters if c["place_name"] is None]
    elif view == "unassigned":
        orphan_items = [
            {**dict(item), "face_tags": json.loads(item["face_tags"])}
            for item in conn.execute(
                "SELECT id, path, item_type, quality_score, face_tags, include_in_video, "
                "is_representative, rotation, clip_start, clip_end, crop, place_name FROM items "
                "WHERE cluster_id IS NULL ORDER BY id"
            ).fetchall()
        ]
    elif sel_day is not None:
        tiles = [summarize(c) for c in clusters_by_day[sel_day["trip_day"]]]

    cluster_choices = [
        {"id": c["id"], "label": f"{c['trip_day']} · {c['place_name'] or 'unresolved'}"}
        for c in clusters
    ]
    enrolled_people = [
        r["person_label"] for r in conn.execute(
            "SELECT DISTINCT person_label FROM enrolled_faces ORDER BY person_label").fetchall()
    ]
    intro_row = conn.execute("SELECT intro_text, outro_text FROM trip_intro WHERE id = 1").fetchone()
    included_count = conn.execute("SELECT COUNT(*) AS n FROM items WHERE include_in_video = 1").fetchone()["n"]
    gap = conn.execute("SELECT gap_threshold_minutes FROM clusters ORDER BY trip_day LIMIT 1").fetchone()

    return templates.TemplateResponse(request, "review.html", {
        "days": days,
        "overview": overview,
        "view": view,
        "sel_day": sel_day,
        "tiles": tiles,
        "day_sections": day_sections,
        "orphan_items": orphan_items,
        "cluster_choices": cluster_choices,
        "enrolled_people": enrolled_people,
        "auto_place_id": config.get_bool("auto_place_id", False),
        "intro_text": intro_row["intro_text"] if intro_row else "",
        "outro_text": intro_row["outro_text"] if intro_row else "",
        "included_count": included_count,
        "current_gap_threshold": gap["gap_threshold_minutes"] if gap else DEFAULT_GAP_THRESHOLD_MINUTES,
    })


# Columns selected for an item wherever a cluster is (re)rendered - kept identical
# to the review_page query so the fragment matches the full-page markup exactly.
_ITEM_COLUMNS = (
    "id, path, item_type, quality_score, face_tags, include_in_video, "
    "is_representative, rotation, clip_start, clip_end, crop, place_name"
)


def _cluster_entry(conn, cluster_id: int):
    """Build the {cluster, media_items} entry for one cluster, or None if it's
    gone (e.g. removed by a re-cluster)."""
    cluster = conn.execute(
        "SELECT id, trip_day, place_name, place_source, consent_status, "
        "music_track, gap_threshold_minutes FROM clusters WHERE id = ?",
        (cluster_id,),
    ).fetchone()
    if cluster is None:
        return None
    items = conn.execute(
        f"SELECT {_ITEM_COLUMNS} FROM items WHERE cluster_id = ? ORDER BY sort_order, timestamp",
        (cluster_id,),
    ).fetchall()
    return {
        "cluster": dict(cluster),
        "media_items": [
            {**dict(item), "face_tags": json.loads(item["face_tags"])} for item in items
        ],
    }


@router.get("/review/clusters/{cluster_id}/fragment", response_class=HTMLResponse)
def cluster_fragment(cluster_id: int, request: Request, conn=Depends(get_db)):
    """Re-render a single moment so the review UI can patch it in place after an
    edit (cover, place, faces, move) instead of reloading the whole page."""
    entry = _cluster_entry(conn, cluster_id)
    if entry is None:
        return HTMLResponse("", status_code=404)
    enrolled_people = [
        row["person_label"]
        for row in conn.execute(
            "SELECT DISTINCT person_label FROM enrolled_faces ORDER BY person_label"
        ).fetchall()
    ]
    return templates.TemplateResponse(
        request, "_cluster.html", {
            "entry": entry,
            "enrolled_people": enrolled_people,
            "music_tracks": [p.name for p in config.ordered_music_files(
                config.get_setting("music_dir"))],
        }
    )


@router.post("/intro")
def save_intro(intro_text: str = Form(...), conn=Depends(get_db)):
    conn.execute(
        "INSERT INTO trip_intro (id, intro_text) VALUES (1, ?) "
        "ON CONFLICT(id) DO UPDATE SET intro_text = excluded.intro_text",
        (intro_text,),
    )
    conn.commit()
    return {"ok": True}


@router.post("/outro")
def save_outro(outro_text: str = Form(...), conn=Depends(get_db)):
    conn.execute(
        "INSERT INTO trip_intro (id, outro_text) VALUES (1, ?) "
        "ON CONFLICT(id) DO UPDATE SET outro_text = excluded.outro_text",
        (outro_text,),
    )
    conn.commit()
    return {"ok": True}


@router.post("/days/{trip_day}/caption")
def save_day_caption(trip_day: str, caption_text: str = Form(...), conn=Depends(get_db)):
    conn.execute(
        "INSERT INTO day_captions (trip_day, caption_text) VALUES (?, ?) "
        "ON CONFLICT(trip_day) DO UPDATE SET caption_text = excluded.caption_text",
        (trip_day, caption_text),
    )
    conn.commit()
    return {"ok": True}


@router.post("/clusters/{cluster_id}/music")
def save_cluster_music(cluster_id: int, track: str = Form(""), conn=Depends(get_db)):
    """Per-moment soundtrack cue: this moment starts the given track (a filename in
    the music folder), which then plays until the next moment that sets one. Empty
    = continue the previous track (stored as NULL). An unknown filename is rejected
    so a stale option can't silently do nothing."""
    if conn.execute("SELECT 1 FROM clusters WHERE id = ?", (cluster_id,)).fetchone() is None:
        raise HTTPException(status_code=404, detail="moment not found")
    chosen = (track or "").strip()
    if chosen:
        known = {p.name for p in config.ordered_music_files(config.get_setting("music_dir"))}
        if chosen not in known:
            raise HTTPException(status_code=400, detail="unknown track")
    conn.execute(
        "UPDATE clusters SET music_track = ? WHERE id = ?",
        (chosen or None, cluster_id),
    )
    conn.commit()
    return {"ok": True, "music_track": chosen or None}


@router.get("/media/{item_id}")
def serve_media(item_id: int, request: Request, full: int = 0, conn=Depends(get_db)):
    row = conn.execute(
        "SELECT path, item_type, rotation, crop FROM items WHERE id = ?", (item_id,)
    ).fetchone()
    if row is None:
        raise HTTPException(status_code=404, detail="item not found")
    rotation = (row["rotation"] or 0) % 360
    crop = None if full else row["crop"]  # crop editor loads the full (uncropped) frame

    # Cache correctly ACROSS projects. Item ids restart at 1 in each project's
    # catalog, so `/media/217` names a different file after a project switch -
    # but the URL is identical, so a plain cacheable response makes the browser
    # show the previous project's cached thumbnail on the tile (the lightbox,
    # which cache-busts its URL, showed the right one - the reported bug). The
    # stored `path` already encodes the project (data/projects/<id>/...), so an
    # ETag over path+mtime+transform is unique per project; `no-cache` forces a
    # cheap revalidation each time, yielding a 304 within a project and a fresh
    # image right after a switch.
    import hashlib
    import os

    try:
        mtime = os.path.getmtime(row["path"])
    except OSError:
        mtime = 0.0
    tag_src = f"{row['path']}|{mtime}|{rotation}|{crop}|{full}"
    etag = 'W/"' + hashlib.sha1(tag_src.encode("utf-8")).hexdigest() + '"'
    cache_headers = {"ETag": etag, "Cache-Control": "no-cache"}
    if request.headers.get("if-none-match") == etag:
        return Response(status_code=304, headers=cache_headers)

    if row["item_type"] == "photo" and (rotation or crop):
        # Serve a rotated/cropped copy so thumbnails + lightbox match the render.
        import io
        import numpy as np
        from PIL import Image
        from ingest.image_utils import load_corrected_rgb, apply_crop

        rgb = load_corrected_rgb(row["path"])
        if rotation:
            rgb = np.rot90(rgb, -(rotation // 90) % 4)
        rgb = apply_crop(rgb, crop)
        buf = io.BytesIO()
        Image.fromarray(rgb).save(buf, format="JPEG", quality=88)
        return Response(content=buf.getvalue(), media_type="image/jpeg", headers=cache_headers)

    resp = FileResponse(row["path"])
    resp.headers["ETag"] = etag
    resp.headers["Cache-Control"] = "no-cache"
    return resp


@router.post("/items/{item_id}/toggle")
def toggle_include(item_id: int, conn=Depends(get_db)):
    conn.execute(
        "UPDATE items SET include_in_video = 1 - include_in_video WHERE id = ?",
        (item_id,),
    )
    conn.commit()
    # Return the new state + fresh total so the UI can update in place (no reload).
    included = conn.execute(
        "SELECT include_in_video FROM items WHERE id = ?", (item_id,)
    ).fetchone()
    total = conn.execute(
        "SELECT COUNT(*) AS n FROM items WHERE include_in_video = 1"
    ).fetchone()["n"]
    return {
        "ok": True,
        "included": bool(included["include_in_video"]) if included else None,
        "included_count": total,
    }


# ---- bulk actions (multi-select in review, Phase 3b) ----
def _affected_clusters(conn, item_ids: list[int]) -> list[int]:
    if not item_ids:
        return []
    qs = ",".join("?" * len(item_ids))
    return [
        r["cluster_id"]
        for r in conn.execute(
            f"SELECT DISTINCT cluster_id FROM items WHERE id IN ({qs}) AND cluster_id IS NOT NULL",
            item_ids,
        ).fetchall()
    ]


def _included_count(conn) -> int:
    return conn.execute("SELECT COUNT(*) AS n FROM items WHERE include_in_video = 1").fetchone()["n"]


@router.post("/items/bulk/include")
def bulk_include(item_ids: list[int] = Form(...), value: int = Form(...), conn=Depends(get_db)):
    """Include (value=1) or exclude (value=0) many items at once. Does NOT re-run
    group-shot selection - a bulk include/exclude is an explicit user choice."""
    val = 1 if value else 0
    affected = _affected_clusters(conn, item_ids)
    conn.executemany(
        "UPDATE items SET include_in_video = ? WHERE id = ?", [(val, i) for i in item_ids]
    )
    conn.commit()
    return {"ok": True, "affected": affected, "included_count": _included_count(conn)}


@router.post("/items/bulk/move")
def bulk_move(item_ids: list[int] = Form(...), cluster_id: int = Form(...), conn=Depends(get_db)):
    """Move many items into one moment. Every item keeps its own include/exclude
    state, so moving photos around never changes what's in the film - the user
    curates that themselves."""
    sources = _affected_clusters(conn, item_ids)
    conn.executemany(
        "UPDATE items SET cluster_id = ? WHERE id = ?", [(cluster_id, i) for i in item_ids]
    )
    conn.commit()
    return {"ok": True, "affected": sorted(set(sources) | {cluster_id})}


@router.post("/items/bulk/place")
def bulk_place(item_ids: list[int] = Form(...), place_name: str = Form(""), conn=Depends(get_db)):
    """Set (or clear) the per-photo place caption on many items at once."""
    value = place_name.strip() or None
    affected = _affected_clusters(conn, item_ids)
    conn.executemany(
        "UPDATE items SET place_name = ?, place_source = ? WHERE id = ?",
        [(value, "manual" if value else None, i) for i in item_ids],
    )
    conn.commit()
    return {"ok": True, "affected": affected}


@router.post("/items/bulk/faces")
def bulk_faces(
    item_ids: list[int] = Form(...),
    label: str = Form(...),
    action: str = Form(...),
    conn=Depends(get_db),
):
    """Add or remove one enrolled person's tag across many items. Only the tags
    change; each item keeps its own include/exclude state (the user decides what's
    in the film, so a face edit never silently drops photos)."""
    valid = {r["person_label"] for r in conn.execute("SELECT DISTINCT person_label FROM enrolled_faces")}
    if label not in valid:
        raise HTTPException(status_code=400, detail=f"label must be one of: {sorted(valid)}")
    if action not in {"add", "remove"}:
        raise HTTPException(status_code=400, detail="action must be 'add' or 'remove'")
    affected = _affected_clusters(conn, item_ids)
    for r in conn.execute(
        f"SELECT id, face_tags FROM items WHERE id IN ({','.join('?' * len(item_ids))})", item_ids
    ).fetchall():
        tags = set(json.loads(r["face_tags"]))
        tags.add(label) if action == "add" else tags.discard(label)
        conn.execute("UPDATE items SET face_tags = ? WHERE id = ?", (json.dumps(sorted(tags)), r["id"]))
    conn.commit()
    return {"ok": True, "affected": affected}


@router.post("/clusters/{cluster_id}/place")
def set_manual_place(cluster_id: int, place_name: str = Form(...), conn=Depends(get_db)):
    """Direct manual place entry - separate from blur-and-send, for when
    you just want to type in where this was rather than involve the cloud
    API at all (or the cloud attempt already failed/was excluded)."""
    conn.execute(
        "UPDATE clusters SET place_name = ?, place_source = 'manual', "
        "consent_status = 'manual' WHERE id = ?",
        (place_name, cluster_id),
    )
    conn.commit()
    return {"ok": True}


@router.post("/items/{item_id}/place")
def set_item_place(item_id: int, place_name: str = Form(""), conn=Depends(get_db)):
    """Set/clear the per-photo place caption (editable under each photo in review).
    Empty clears it."""
    value = place_name.strip() or None
    conn.execute(
        "UPDATE items SET place_name = ?, place_source = ? WHERE id = ?",
        (value, "manual" if value else None, item_id),
    )
    conn.commit()
    return {"ok": True}


@router.post("/items/{item_id}/move-to-cluster")
def move_item_to_cluster(item_id: int, cluster_id: int = Form(...), conn=Depends(get_db)):
    """Manual override for a misclustered item - drag it into a different
    moment's grid in the review UI. The item keeps its own include/exclude state;
    moving it never changes what's in the film."""
    row = conn.execute("SELECT 1 FROM items WHERE id = ?", (item_id,)).fetchone()
    if row is None:
        raise HTTPException(status_code=404, detail="item not found")

    conn.execute("UPDATE items SET cluster_id = ? WHERE id = ?", (cluster_id, item_id))
    conn.commit()
    return {"ok": True}


@router.post("/clusters/{cluster_id}/cover/{item_id}")
def set_cover(cluster_id: int, item_id: int, conn=Depends(get_db)):
    """Make this photo the stop's cover: its representative (the ★ badge / cloud-ID
    candidate) AND the lead item, so it carries the place caption. Moves it to the
    front of the stop and makes sure it's included. Doesn't touch the other
    photos' include state."""
    row = conn.execute(
        "SELECT item_type FROM items WHERE id = ? AND cluster_id = ?", (item_id, cluster_id)
    ).fetchone()
    if row is None:
        raise HTTPException(status_code=404, detail="item not found in this stop")
    if row["item_type"] != "photo":
        raise HTTPException(status_code=400, detail="only a photo can be the cover")

    conn.execute("UPDATE items SET is_representative = 0 WHERE cluster_id = ?", (cluster_id,))
    conn.execute(
        "UPDATE items SET is_representative = 1, include_in_video = 1 WHERE id = ?", (item_id,)
    )
    # Lead the stop: renumber sort_order with this item first.
    ids = [
        r["id"]
        for r in conn.execute(
            "SELECT id FROM items WHERE cluster_id = ? ORDER BY sort_order, timestamp",
            (cluster_id,),
        ).fetchall()
    ]
    ids = [item_id] + [i for i in ids if i != item_id]
    conn.executemany(
        "UPDATE items SET sort_order = ? WHERE id = ?", [(order, i) for order, i in enumerate(ids)]
    )
    conn.commit()
    return {"ok": True}


@router.post("/items/{item_id}/rotate")
def rotate_item(item_id: int, conn=Depends(get_db)):
    """Rotate a photo 90° clockwise (lightbox). Non-destructive - the angle is
    stored and applied on serve + at render; the original file is untouched."""
    row = conn.execute("SELECT rotation, item_type FROM items WHERE id = ?", (item_id,)).fetchone()
    if row is None:
        raise HTTPException(status_code=404, detail="item not found")
    if row["item_type"] != "photo":
        raise HTTPException(status_code=400, detail="only photos can be rotated")
    new_rotation = ((row["rotation"] or 0) + 90) % 360
    conn.execute("UPDATE items SET rotation = ? WHERE id = ?", (new_rotation, item_id))
    conn.commit()
    return {"ok": True, "rotation": new_rotation}


@router.post("/items/{item_id}/trim")
def trim_item(item_id: int, start: float = Form(0.0), end: float = Form(0.0), conn=Depends(get_db)):
    """Set a clip's in/out points (lightbox). end<=start (or 0) means "to the
    end". Non-destructive - applied at render via subclipped."""
    row = conn.execute("SELECT item_type FROM items WHERE id = ?", (item_id,)).fetchone()
    if row is None:
        raise HTTPException(status_code=404, detail="item not found")
    if row["item_type"] != "clip":
        raise HTTPException(status_code=400, detail="only clips can be trimmed")
    start = max(0.0, start)
    end_value = end if end > start else None  # None = play to the natural end
    conn.execute(
        "UPDATE items SET clip_start = ?, clip_end = ? WHERE id = ?",
        (start, end_value, item_id),
    )
    conn.commit()
    return {"ok": True}


@router.post("/items/{item_id}/crop")
def crop_item(item_id: int, crop: str = Form(""), conn=Depends(get_db)):
    """Set (or clear) a photo's crop, as a normalized "x,y,w,h" string relative
    to the rotation-corrected frame (lightbox). Empty clears it. Non-destructive."""
    row = conn.execute("SELECT item_type FROM items WHERE id = ?", (item_id,)).fetchone()
    if row is None:
        raise HTTPException(status_code=404, detail="item not found")
    if row["item_type"] != "photo":
        raise HTTPException(status_code=400, detail="only photos can be cropped")
    value = crop.strip() or None
    if value:
        try:
            parts = [float(v) for v in value.split(",")]
            if len(parts) != 4 or not all(0 <= p <= 1 for p in parts) or parts[2] <= 0 or parts[3] <= 0:
                raise ValueError
        except ValueError:
            raise HTTPException(status_code=400, detail="invalid crop")
    conn.execute("UPDATE items SET crop = ? WHERE id = ?", (value, item_id))
    conn.commit()
    return {"ok": True}


@router.post("/reorder")
def reorder_items(item_ids_in_order: list[int] = Form(...), conn=Depends(get_db)):
    conn.executemany(
        "UPDATE items SET sort_order = ? WHERE id = ?",
        [(order, item_id) for order, item_id in enumerate(item_ids_in_order)],
    )
    conn.commit()
    return {"ok": True}


@router.post("/items/{item_id}/move")
def move_item_within_cluster(item_id: int, direction: str = Form(...), conn=Depends(get_db)):
    """Nudge an item one position earlier/later within its own cluster via
    the ◀/▶ buttons - a reliable alternative to drag-and-drop. Rewrites
    sort_order for the *whole* cluster to a contiguous 0..n so the manual
    order actually takes effect at generate time (items left at NULL
    sort_order otherwise sort ahead of any reordered ones)."""
    if direction not in ("left", "right"):
        raise HTTPException(status_code=400, detail="direction must be 'left' or 'right'")
    row = conn.execute("SELECT cluster_id FROM items WHERE id = ?", (item_id,)).fetchone()
    if row is None or row["cluster_id"] is None:
        raise HTTPException(status_code=404, detail="item not found or has no cluster")

    ids = [
        r["id"]
        for r in conn.execute(
            "SELECT id FROM items WHERE cluster_id = ? ORDER BY sort_order, timestamp",
            (row["cluster_id"],),
        ).fetchall()
    ]
    idx = ids.index(item_id)
    swap = idx - 1 if direction == "left" else idx + 1
    if 0 <= swap < len(ids):
        ids[idx], ids[swap] = ids[swap], ids[idx]
    conn.executemany(
        "UPDATE items SET sort_order = ? WHERE id = ?",
        [(order, iid) for order, iid in enumerate(ids)],
    )
    conn.commit()
    return {"ok": True}


# ---- day / moment management (Story workspace: Add Day / Add Moment / delete) ----
@router.post("/clusters/add")
def add_cluster(trip_day: str = Form(...), conn=Depends(get_db)):
    """Create an empty moment in a day; the user drags photos into it or gives it a
    place. Inherits the day's gap threshold if it has one."""
    gap = conn.execute(
        "SELECT gap_threshold_minutes FROM clusters WHERE trip_day = ? LIMIT 1", (trip_day,)
    ).fetchone()
    gap_val = gap["gap_threshold_minutes"] if gap else DEFAULT_GAP_THRESHOLD_MINUTES
    cur = conn.execute(
        "INSERT INTO clusters (trip_day, gap_threshold_minutes) VALUES (?, ?)", (trip_day, gap_val)
    )
    conn.commit()
    return {"ok": True, "cluster_id": cur.lastrowid}


@router.get("/browse-media")
def browse_media(path: str = ""):
    """Read-only folder browser behind the 'Add photos or clips' picker: lists
    the sub-folders you can navigate into and the photo/clip files in the current
    folder that you can pick individually. Never writes. Mirrors /setup/browse but
    also returns files, so the user can see and choose what to add (like a file
    explorer) instead of typing a path."""
    base = Path(path) if path else Path.home()
    dirs: list[dict] = []
    files: list[dict] = []
    try:
        base = base.resolve()
        if not base.is_dir():
            base = Path.home().resolve()
        for p in sorted(base.iterdir(), key=lambda x: x.name.lower()):
            if p.name.startswith("."):
                continue
            if p.is_dir():
                dirs.append({"name": p.name, "path": str(p)})
            else:
                kind = classify_media(p)
                if kind is not None:
                    files.append({"name": p.name, "path": str(p), "type": kind})
    except (OSError, PermissionError):
        dirs, files = [], []
    parent = str(base.parent) if base.parent != base else None
    return {"path": str(base), "parent": parent, "dirs": dirs, "files": files}


@router.post("/clusters/{cluster_id}/add-files")
def add_cluster_files(cluster_id: int, paths: list[str] = Form(...), conn=Depends(get_db)):
    """Add the specific photos/clips the user picked in the media browser into this
    moment. `paths` are individual file paths chosen in the picker. Lets the user
    pull in media that wasn't part of the original import (e.g. a clip downloaded
    from the web), including into a moment they created by hand."""
    if conn.execute("SELECT 1 FROM clusters WHERE id = ?", (cluster_id,)).fetchone() is None:
        raise HTTPException(status_code=404, detail="moment not found")
    chosen = [p.strip() for p in paths if p.strip()]
    if not chosen:
        raise HTTPException(status_code=400, detail="select at least one photo or clip")
    enrolled = load_enrolled(conn)
    added: list[int] = []
    skipped: list = []
    duplicates: list = []
    for p in chosen:
        try:
            result = add_items_from_path(conn, cluster_id, p, enrolled)
        except FileNotFoundError:
            skipped.append((p, "not found"))
            continue
        added += result["added"]
        duplicates += result["duplicates"]
        skipped += result["skipped"]
    if not added and not duplicates and not skipped:
        raise HTTPException(status_code=400, detail="nothing to add")
    return {"ok": True, "added": added, "skipped": skipped, "duplicates": duplicates}


@router.post("/clusters/{cluster_id}/delete")
def delete_cluster(cluster_id: int, conn=Depends(get_db)):
    """Delete a moment: its photos move to Unassigned (cluster_id NULL), not deleted."""
    conn.execute("UPDATE items SET cluster_id = NULL WHERE cluster_id = ?", (cluster_id,))
    conn.execute("DELETE FROM clusters WHERE id = ?", (cluster_id,))
    conn.commit()
    return {"ok": True}


@router.post("/clusters/{cluster_id}/merge")
def merge_cluster(cluster_id: int, target_id: int = Form(...), conn=Depends(get_db)):
    """Merge this moment into another: move all its items to the target and
    delete the now-empty source. Each item keeps its own include/exclude state,
    so the user's curation in both moments survives the merge (we deliberately
    do NOT re-run group-shot selection, which would collapse the merged moment
    back down to a single included photo)."""
    if cluster_id == target_id:
        raise HTTPException(status_code=400, detail="can't merge a moment into itself")
    if conn.execute("SELECT 1 FROM clusters WHERE id = ?", (target_id,)).fetchone() is None:
        raise HTTPException(status_code=404, detail="target moment not found")
    conn.execute("UPDATE items SET cluster_id = ? WHERE cluster_id = ?", (target_id, cluster_id))
    conn.execute("DELETE FROM clusters WHERE id = ?", (cluster_id,))
    conn.commit()
    return {"ok": True}


@router.post("/days/add")
def add_day(conn=Depends(get_db)):
    """Add a new day (dated the day after the current last one, else today) with one
    empty moment so it shows up in the rail."""
    row = conn.execute("SELECT MAX(trip_day) AS m FROM clusters").fetchone()
    new_day = date.today()
    if row and row["m"]:
        try:
            new_day = datetime.strptime(row["m"], "%Y-%m-%d").date() + timedelta(days=1)
        except ValueError:
            pass
    td = new_day.isoformat()
    conn.execute(
        "INSERT INTO clusters (trip_day, gap_threshold_minutes) VALUES (?, ?)",
        (td, DEFAULT_GAP_THRESHOLD_MINUTES),
    )
    conn.commit()
    return {"ok": True, "trip_day": td}


@router.post("/days/{trip_day}/delete")
def delete_day(trip_day: str, conn=Depends(get_db)):
    """Delete a day: all its moments dissolve and their photos move to Unassigned."""
    conn.execute(
        "UPDATE items SET cluster_id = NULL WHERE cluster_id IN "
        "(SELECT id FROM clusters WHERE trip_day = ?)", (trip_day,)
    )
    conn.execute("DELETE FROM clusters WHERE trip_day = ?", (trip_day,))
    conn.execute("DELETE FROM day_captions WHERE trip_day = ?", (trip_day,))
    conn.commit()
    return {"ok": True}


@router.post("/recluster")
def recluster(gap_threshold_minutes: float = Form(...), conn=Depends(get_db)):
    """Re-clustering only makes sense before place resolution has happened
    (Premise 8: tuned right after the first ingest run) - clusters that
    already have a resolved place or pending consent are left untouched."""
    conn.execute(
        "UPDATE items SET cluster_id = NULL WHERE cluster_id IN "
        "(SELECT id FROM clusters WHERE place_name IS NULL AND consent_status = 'pending')"
    )
    conn.execute(
        "DELETE FROM clusters WHERE place_name IS NULL AND consent_status = 'pending'"
    )
    conn.commit()
    ingest_phase2(conn, gap_threshold_minutes)
    # No automatic location resolution - route the re-formed moments to manual/
    # on-demand place entry, same as import.
    conn.execute(
        "UPDATE clusters SET consent_status = 'manual' "
        "WHERE place_name IS NULL AND consent_status = 'pending'"
    )
    conn.commit()
    return {"ok": True}


@router.post("/clusters/{cluster_id}/blur-and-send")
def blur_and_send_route(cluster_id: int, conn=Depends(get_db)):
    try:
        blur_and_send(conn, cluster_id)
    except PlaceIdApiError as e:
        raise HTTPException(status_code=502, detail=str(e))
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    return {"ok": True}


@router.post("/items/{item_id}/send-for-id")
def send_for_id_route(item_id: int, conn=Depends(get_db)):
    """On-demand per-photo location search (GPS first, then the vision AI)."""
    try:
        place_name = find_location_for_item(conn, item_id)
    except PlaceIdApiError as e:
        raise HTTPException(status_code=502, detail=str(e))
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    return {"ok": True, "place_name": place_name}


@router.post("/generate")
def trigger_generate(mode: str = Form("final"), render: str = Form(""), conn=Depends(get_db)):
    included = conn.execute(
        "SELECT COUNT(*) AS n FROM items WHERE include_in_video = 1"
    ).fetchone()["n"]
    if included == 0:
        raise HTTPException(
            status_code=400,
            detail="No items included - include at least one photo or clip before generating.",
        )
    if _gen_get()["state"] == "running":
        raise HTTPException(status_code=409, detail="A video is already being generated.")

    render_mode = "draft" if mode == "draft" else "final"
    # Single vs. multiprocessor final render (a standard user choice). Remember the
    # pick as the default; when unspecified, generate_video uses the saved default.
    render = (render or "").strip().lower()
    if render in ("single", "multi"):
        render_parallel = render == "multi"
        config.set_setting("render_parallel", render_parallel)
    else:
        render_parallel = None
    _gen_set(state="running", done=0, total=0, message="Starting…", output="", output_name="", elapsed="")

    def job():
        c = get_connection(DEFAULT_DB_PATH)
        started = time.perf_counter()
        try:
            from generate.assemble import generate_video

            def cb(done, total, message):
                kw = {"message": message}
                if done is not None:
                    kw["done"] = done
                if total is not None:
                    kw["total"] = total
                _gen_set(**kw)

            # Name the file after the project being rendered right now (the open
            # project), not a stale global setting.
            out = generate_video(c, progress_cb=cb, mode=render_mode,
                                 basename=project_store.output_basename(),
                                 render_parallel=render_parallel)
            # A draft is a throwaway preview; only a full-quality render marks the
            # project "finished" (and only that gets the finished badge/flow).
            if render_mode == "final":
                project_store.mark_video(out.name)
            secs = int(round(time.perf_counter() - started))
            _gen_set(state="done", message="Done", output=str(out),
                     output_name=out.name, mode=render_mode,
                     elapsed=f"{secs // 60}m{secs % 60:02d}s")
        except Exception as e:  # surfaced to the UI, never crashes the server
            _gen_set(state="error", message=f"{type(e).__name__}: {e}")
        finally:
            c.close()

    threading.Thread(target=job, daemon=True).start()
    return {"ok": True}


@router.get("/generate/status")
def generate_status():
    return _gen_get()


@router.get("/output/{name}")
def serve_output(name: str):
    """Serve a finished video from the configured output folder so the review
    page can link to it. Basename-only (Path(name).name) to prevent path
    traversal outside the output folder."""
    output_dir = Path(config.get_setting("output_dir", str(Path("data") / "output")))
    path = output_dir / Path(name).name
    if not path.is_file():
        raise HTTPException(status_code=404, detail="video not found")
    return FileResponse(str(path), media_type="video/mp4", filename=path.name)


def _latest_output() -> str | None:
    """Filename of the CURRENT project's finished film, or None.

    The film is scoped to the open project (project_store records it via
    mark_video on a full render). We deliberately do NOT fall back to "the
    newest mp4 in the folder": the output folder is shared across every
    project, so a global 'latest wins' lookup would show another story's film
    on the Finish/Preview pages of a story that hasn't been rendered yet. When
    the current project has no film, we show the empty 'no film yet' state.
    Draft previews (…_draft_….mp4) are never recorded, so they never surface
    here (a just-built draft still shows live via the generation status)."""
    proj = project_store.current()
    name = (proj or {}).get("video")
    if not name or "_draft_" in name:
        return None
    output_dir = Path(config.get_setting("output_dir", str(Path("data") / "output")))
    path = output_dir / Path(name).name
    return path.name if path.is_file() else None


@router.get("/preview", response_class=HTMLResponse)
def preview_page(request: Request, conn=Depends(get_db)):
    """Watch the current draft; rebuild it after editing in Story."""
    included = conn.execute("SELECT COUNT(*) AS n FROM items WHERE include_in_video = 1").fetchone()["n"]
    return templates.TemplateResponse(request, "preview.html", {
        "latest": _latest_output(),
        "included_count": included,
        "render_parallel": config.get_bool("render_parallel", True),
    })


@router.get("/finish", response_class=HTMLResponse)
def finish_page(request: Request, conn=Depends(get_db)):
    """The finished film: play + download; nothing leaves the machine."""
    included = conn.execute("SELECT COUNT(*) AS n FROM items WHERE include_in_video = 1").fetchone()["n"]
    return templates.TemplateResponse(request, "finish.html", {
        "latest": _latest_output(),
        "included_count": included,
        "output_dir": config.get_setting("output_dir", str(Path("data") / "output")),
    })
