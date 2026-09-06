"""Ingest pipeline orchestrator (Next Steps step 5, Premise 20).

Phase 1: stream one file at a time - decode once, tag faces + score quality
together, extract EXIF/timestamp, insert into the catalog. Corrupt files are
skipped and logged (Premise 16), not fatal.

Phase 2: cluster by timestamp gap (cheap, EXIF-timestamp only, no decode
needed), resolve each cluster's trip_day, and run group-shot selection -
all after every item's metadata is already known. Cloud place-ID (step 6)
runs as a separate stage after phase 2, once clusters exist.
"""

import json
import sqlite3
from datetime import date, datetime, timedelta
from pathlib import Path

from db.schema import get_connection, DEFAULT_DB_PATH
from ingest.dates import media_capture_date, in_window
from ingest.exif import parse_exif, read_clip_timestamp, UnreadableFileError
from ingest.image_utils import load_corrected_bgr_with_exif
from ingest.faces import tag_faces
from ingest.quality import score_photo_quality, score_clip_quality
from ingest.clustering import (
    cluster_by_gap,
    cluster_trip_day,
    DEFAULT_GAP_THRESHOLD_MINUTES,
)
from cloud.resolve_places import resolve_gps_items

PHOTO_EXTENSIONS = {".jpg", ".jpeg", ".png"}
CLIP_EXTENSIONS = {".mp4", ".mov", ".avi"}


def classify_media(path: Path) -> str | None:
    """'photo' | 'clip' by extension, or None if it isn't media we handle."""
    ext = path.suffix.lower()
    if ext in PHOTO_EXTENSIONS:
        return "photo"
    if ext in CLIP_EXTENSIONS:
        return "clip"
    return None


def _probe_media_file(path: Path, item_type: str, enrolled: dict) -> dict:
    """Decode/score/tag one file and return its catalog metadata fields. Raises
    UnreadableFileError on a corrupt/unreadable file. Shared by the folder import
    (ingest_phase1) and the manual "add from path" flow so both handle a photo or
    clip identically."""
    gps_lat = gps_lon = None
    if item_type == "photo":
        try:
            image, exif = load_corrected_bgr_with_exif(str(path))  # one decode
        except Exception as e:  # corrupt/truncated/unreadable image
            raise UnreadableFileError(f"{path}: {e}") from e
        exif_result = parse_exif(exif)
        # Skip the (heavy) face model entirely when nobody is enrolled - no
        # enrolled reference means no tag could ever match anyway.
        face_tags = tag_faces(image, enrolled) if enrolled else []
        quality_score = score_photo_quality(image)
        timestamp = exif_result.timestamp
        if exif_result.gps is not None:
            gps_lat, gps_lon = exif_result.gps
    else:
        timestamp = read_clip_timestamp(str(path))
        quality_result = score_clip_quality(str(path))
        quality_score = 1.0 if quality_result.passes else 0.0
        face_tags = []  # clips exempt from face logic (Premise 23)
    return {
        "timestamp": timestamp, "gps_lat": gps_lat, "gps_lon": gps_lon,
        "face_tags": face_tags, "quality_score": quality_score,
    }


def _iter_media_files(folder: Path, exclude_dirs: set[str] | None = None):
    all_extensions = PHOTO_EXTENSIONS | CLIP_EXTENSIONS
    exclude_dirs = exclude_dirs or set()
    for path in sorted(folder.rglob("*")):
        if not path.is_file() or path.suffix.lower() not in all_extensions:
            continue
        if any(part in exclude_dirs for part in path.relative_to(folder).parts[:-1]):
            continue  # skips files under any excluded subfolder, e.g. a "test" folder
        yield path


def ingest_phase1(folder: Path, enrolled: dict, conn, exclude_dirs: set[str] | None = None,
                  progress_cb=None, date_start: date | None = None,
                  date_end: date | None = None) -> dict:
    """Stream every file once: decode, tag, score, insert. Corrupt files
    are skipped and logged, ingest continues (Premise 16).

    `progress_cb(processed, total, message)` (optional) is called once per file
    so the Setup UI can show an import progress bar.

    When `date_start`/`date_end` are given, a cheap first pass dates every file
    (EXIF/clip timestamp, no pixel decode) and drops those taken outside the
    inclusive window BEFORE the expensive decode/tag/score pass - so pointing at
    a whole camera roll doesn't process months of unrelated photos to keep a
    trip's worth. Undated files (no reliable capture date) are kept. Returns
    {imported_photos, imported_clips, skipped_out_of_range, included_undated}."""
    # Pass 1 (cheap): date + partition. Undated stay in scope (included policy).
    in_scope: list[tuple[Path, date | None]] = []
    skipped_out_of_range = 0
    included_undated = 0
    for path in _iter_media_files(folder, exclude_dirs):
        d = media_capture_date(path)
        if in_window(d, date_start, date_end):
            in_scope.append((path, d))
            if d is None:
                included_undated += 1
        else:
            skipped_out_of_range += 1

    # Pass 2 (heavy): decode/tag/score/insert only the survivors.
    imported_photos = imported_clips = 0
    total = len(in_scope)
    for idx, (path, _d) in enumerate(in_scope, start=1):
        if progress_cb is not None:
            progress_cb(idx, total, f"Importing {path.name}")
        item_type = classify_media(path) or "clip"
        try:
            fields = _probe_media_file(path, item_type, enrolled)
        except UnreadableFileError as e:
            conn.execute(
                "INSERT INTO skipped_files (path, reason) VALUES (?, ?)",
                (str(path), str(e)),
            )
            continue

        # Everything starts included. The first time a trip is loaded into review,
        # every photo and clip is in the film; the user then curates DOWN by
        # excluding what they don't want, rather than the tool pre-pruning for them.
        # Quality score is still stored (for cover choice and ordering) - it just no
        # longer decides inclusion.
        cur = conn.execute(
            """INSERT OR IGNORE INTO items
               (path, item_type, timestamp, gps_lat, gps_lon, face_tags, quality_score, include_in_video)
               VALUES (?, ?, ?, ?, ?, ?, ?, 1)""",
            (
                str(path), item_type, fields["timestamp"], fields["gps_lat"],
                fields["gps_lon"], json.dumps(fields["face_tags"]), fields["quality_score"],
            ),
        )
        if cur.rowcount:  # 0 when INSERT OR IGNORE skipped a duplicate path
            if item_type == "photo":
                imported_photos += 1
            else:
                imported_clips += 1
    conn.commit()
    return {
        "imported_photos": imported_photos,
        "imported_clips": imported_clips,
        "skipped_out_of_range": skipped_out_of_range,
        "included_undated": included_undated,
    }


def add_items_from_path(conn, cluster_id: int, path: str, enrolled: dict) -> dict:
    """Manually add media into an existing moment from a user-supplied path.

    `path` may be a single photo/clip file OR a folder (every photo/clip inside it
    is added). Each new item is included by default and appended after the moment's
    current items. Timestamp falls back to the moment's latest item time (else its
    day at noon), so a clip with no embedded time still lands inside this moment
    rather than sorting to the front. Returns {"added": [ids], "skipped":
    [(name, reason)], "duplicates": [names]} so the caller can report the outcome.

    Reads local files the user points at - this is a single-user local tool, so the
    path is trusted (their own machine)."""
    p = Path(path).expanduser()
    if not p.exists():
        raise FileNotFoundError(path)

    if p.is_dir():
        candidates = list(_iter_media_files(p))
    elif classify_media(p) is not None:
        candidates = [p]
    else:
        candidates = []  # a real file, but not a photo/clip we handle

    max_so = conn.execute(
        "SELECT COALESCE(MAX(sort_order), -1) AS mx FROM items WHERE cluster_id = ?",
        (cluster_id,),
    ).fetchone()["mx"]
    ts_row = conn.execute(
        "SELECT MAX(timestamp) AS mx FROM items WHERE cluster_id = ? AND timestamp IS NOT NULL",
        (cluster_id,),
    ).fetchone()
    fallback_ts = ts_row["mx"] if ts_row else None
    if not fallback_ts:
        day_row = conn.execute("SELECT trip_day FROM clusters WHERE id = ?", (cluster_id,)).fetchone()
        fallback_ts = (
            f"{day_row['trip_day']}T12:00:00"
            if day_row and day_row["trip_day"] else datetime.now().isoformat()
        )

    added, skipped, duplicates = [], [], []
    next_order = max_so + 1
    for f in candidates:
        item_type = classify_media(f)
        if item_type is None:
            continue
        if conn.execute("SELECT 1 FROM items WHERE path = ?", (str(f),)).fetchone():
            duplicates.append(f.name)  # already in the catalog; don't re-add or move it
            continue
        try:
            fields = _probe_media_file(f, item_type, enrolled)
        except UnreadableFileError as e:
            conn.execute("INSERT INTO skipped_files (path, reason) VALUES (?, ?)", (str(f), str(e)))
            skipped.append((f.name, "unreadable"))
            continue
        try:
            cur = conn.execute(
                """INSERT INTO items
                   (path, item_type, timestamp, gps_lat, gps_lon, face_tags,
                    quality_score, include_in_video, cluster_id, sort_order)
                   VALUES (?, ?, ?, ?, ?, ?, ?, 1, ?, ?)""",
                (
                    str(f), item_type, fields["timestamp"] or fallback_ts, fields["gps_lat"],
                    fields["gps_lon"], json.dumps(fields["face_tags"]), fields["quality_score"],
                    cluster_id, next_order,
                ),
            )
        except sqlite3.IntegrityError:
            duplicates.append(f.name)
            continue
        added.append(cur.lastrowid)
        next_order += 1

    conn.commit()
    return {"added": added, "skipped": skipped, "duplicates": duplicates}


def _find_adjacent_existing_cluster(conn, orphan_ts, gap_threshold_minutes: float):
    """Returns an existing cluster's id if orphan_ts falls within the gap
    threshold of that cluster's time range, else None. Without this, an
    item ingested AFTER its original batch (e.g. a clip re-ingested with a
    corrected timestamp) always formed its own isolated cluster, even when
    it belonged in an already-existing one - clustering previously only
    ever considered items that didn't have a cluster yet."""
    threshold = timedelta(minutes=gap_threshold_minutes)
    existing = conn.execute(
        "SELECT cluster_id, MIN(timestamp) AS min_ts, MAX(timestamp) AS max_ts "
        "FROM items WHERE cluster_id IS NOT NULL AND timestamp IS NOT NULL "
        "GROUP BY cluster_id"
    ).fetchall()
    for row in existing:
        min_ts = datetime.fromisoformat(row["min_ts"])
        max_ts = datetime.fromisoformat(row["max_ts"])
        if min_ts - threshold <= orphan_ts <= max_ts + threshold:
            return row["cluster_id"]
    return None


def ingest_phase2(
    conn,
    gap_threshold_minutes: float = DEFAULT_GAP_THRESHOLD_MINUTES,
) -> None:
    """Cluster by timestamp gap and resolve each cluster's trip_day. Every item
    keeps the include_in_video state phase 1 set (all included by default) - we do
    NOT auto-collapse a moment down to a single "group shot" anymore; the user
    curates which photos to drop in review. Place resolution (cloud API / manual)
    is a separate stage (step 6).

    Orphan items are first checked against EXISTING clusters and merged in
    if temporally adjacent, before falling back to grouping the remaining
    orphans among themselves into new clusters."""
    rows = conn.execute(
        "SELECT id, timestamp, item_type, quality_score, face_tags "
        "FROM items WHERE timestamp IS NOT NULL AND cluster_id IS NULL"
    ).fetchall()

    still_orphan_rows = []
    for row in rows:
        orphan_ts = datetime.fromisoformat(row["timestamp"])
        existing_cluster_id = _find_adjacent_existing_cluster(conn, orphan_ts, gap_threshold_minutes)
        if existing_cluster_id is not None:
            conn.execute(
                "UPDATE items SET cluster_id = ? WHERE id = ?", (existing_cluster_id, row["id"])
            )
        else:
            still_orphan_rows.append(row)
    conn.commit()

    rows = still_orphan_rows
    items_with_ts = [
        (row["id"], datetime.fromisoformat(row["timestamp"])) for row in rows
    ]
    assignments = cluster_by_gap(items_with_ts, gap_threshold_minutes)

    by_cluster: dict[int, list] = {}
    for row in rows:
        cluster_idx = assignments.get(row["id"])
        if cluster_idx is None:
            continue
        by_cluster.setdefault(cluster_idx, []).append(row)

    for cluster_rows in by_cluster.values():
        timestamps = [datetime.fromisoformat(r["timestamp"]) for r in cluster_rows]
        trip_day = cluster_trip_day(timestamps)
        cursor = conn.execute(
            "INSERT INTO clusters (trip_day, gap_threshold_minutes) VALUES (?, ?)",
            (trip_day, gap_threshold_minutes),
        )
        cluster_id = cursor.lastrowid
        conn.executemany(
            "UPDATE items SET cluster_id = ? WHERE id = ?",
            [(cluster_id, r["id"]) for r in cluster_rows],
        )

    conn.commit()


def run_ingest(
    folder: str,
    enrolled: dict,
    gap_threshold_minutes: float = DEFAULT_GAP_THRESHOLD_MINUTES,
    db_path: Path = DEFAULT_DB_PATH,
    exclude_dirs: set[str] | None = None,
    progress_cb=None,
    date_start: date | None = None,
    date_end: date | None = None,
) -> dict:
    """Fails loudly before any per-file work if the cloud API key is missing
    (Premise 24) - avoids wasting a whole run's worth of place-ID attempts
    on an avoidable, instantly-detectable misconfiguration.

    `progress_cb(processed, total, message)` (optional) reports per-file progress
    during phase 1 and coarse stage messages (processed/total None) afterwards.

    Location resolution is NOT part of import: every moment starts place-less and
    the user searches locations on demand, per included photo, in review (GPS first,
    then the vision AI). Import therefore needs no API key or country - those are
    only checked when the user triggers a per-photo search."""
    def _stage(message):
        if progress_cb is not None:
            progress_cb(None, None, message)

    conn = get_connection(db_path)
    try:
        counts = ingest_phase1(
            Path(folder), enrolled, conn, exclude_dirs, progress_cb=progress_cb,
            date_start=date_start, date_end=date_end,
        )
        _stage("Grouping photos into moments…")
        ingest_phase2(conn, gap_threshold_minutes)
        # Free, offline place labels for GPS-tagged photos: each geotagged item gets
        # its own "City, Region" caption from its coordinates (no API key, nothing
        # leaves the machine). Photos without GPS stay place-less; the per-photo
        # "Find location"/AI button still upgrades any photo to a finer landmark.
        _stage("Reading photo locations…")
        resolve_gps_items(conn)
        # No cloud location phase - route every still-place-less moment to
        # manual/on-demand place entry.
        conn.execute(
            "UPDATE clusters SET consent_status = 'manual' "
            "WHERE place_name IS NULL AND consent_status = 'pending'"
        )
        conn.commit()
        _stage("Done")
        return counts
    finally:
        conn.close()
