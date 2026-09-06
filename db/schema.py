"""SQLite schema for the trip video catalog.

WAL journal mode is enabled so the ingest script and the review server can
access the same database file concurrently without "database is locked"
errors (Premise 14).
"""

import sqlite3
from pathlib import Path

DEFAULT_DB_PATH = Path("data") / "catalog.db"


def get_connection(db_path: Path = DEFAULT_DB_PATH) -> sqlite3.Connection:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    # check_same_thread=False: FastAPI's sync-dependency threadpool doesn't
    # guarantee the same worker thread handles a connection's creation, use,
    # and teardown - sqlite3 otherwise raises ProgrammingError across those.
    # Safe here since each request gets its own connection (review_app/db.py),
    # never shared across concurrent requests.
    conn = sqlite3.connect(db_path, check_same_thread=False)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    # busy_timeout: wait up to 5s for a lock to clear instead of failing
    # instantly with "database is locked" under concurrent writes.
    conn.execute("PRAGMA busy_timeout=5000")
    conn.row_factory = sqlite3.Row
    return conn


SCHEMA = """
CREATE TABLE IF NOT EXISTS enrolled_faces (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    person_label TEXT NOT NULL,          -- 'user' or 'wife'
    embedding BLOB NOT NULL,             -- serialized face embedding vector
    reference_photo_path TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS clusters (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    trip_day TEXT NOT NULL,              -- ISO date, e.g. '2026-05-12'
    gap_threshold_minutes REAL NOT NULL,
    place_name TEXT,
    place_source TEXT,                   -- 'gps' | 'cloud_api' | 'manual' | NULL (unresolved)
    consent_status TEXT NOT NULL DEFAULT 'pending',
        -- 'pending' | 'approved' | 'excluded' | 'failed' | 'manual' | 'not_needed' (GPS path, Premise 21)
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS items (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    path TEXT NOT NULL UNIQUE,
    item_type TEXT NOT NULL CHECK (item_type IN ('photo', 'clip')),
    timestamp TEXT,                      -- ISO datetime from EXIF, nullable if unreadable
    gps_lat REAL,                        -- nullable, present only if EXIF GPS was found
    gps_lon REAL,
    cluster_id INTEGER REFERENCES clusters(id),
    face_tags TEXT NOT NULL DEFAULT '[]', -- JSON array, e.g. ["user","wife"]
    quality_score REAL,
    is_representative INTEGER NOT NULL DEFAULT 0,  -- chosen as cluster's cloud place-ID candidate
    include_in_video INTEGER NOT NULL DEFAULT 1,
    sort_order INTEGER,
    duration_override REAL,              -- nullable, seconds (pacing_design.md)
    rotation INTEGER NOT NULL DEFAULT 0, -- user photo rotation, degrees clockwise (lightbox)
    clip_start REAL,                     -- clip trim in-point, seconds (nullable = 0)
    clip_end REAL,                       -- clip trim out-point, seconds (nullable = full)
    crop TEXT,                           -- normalized "x,y,w,h" crop of the corrected photo (nullable = full)
    place_name TEXT,                     -- per-photo place caption (review "Find place"); shown on THIS photo at generate
    place_source TEXT,                   -- 'gps' | 'cloud_api' | 'cloud_api_blurred'
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS day_captions (
    trip_day TEXT PRIMARY KEY,           -- ISO date, matches clusters.trip_day
    caption_text TEXT NOT NULL           -- editable in the review UI; shown as a
                                          -- title card on the first photo of that day
);

CREATE TABLE IF NOT EXISTS trip_intro (
    id INTEGER PRIMARY KEY CHECK (id = 1),  -- single row, enforced by the CHECK
    intro_text TEXT NOT NULL DEFAULT '',
    outro_text TEXT NOT NULL DEFAULT ''     -- closing slide shown as the final
                                            -- frame of the video (editable in UI)
);

CREATE TABLE IF NOT EXISTS skipped_files (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    path TEXT NOT NULL,
    reason TEXT NOT NULL,
    skipped_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE INDEX IF NOT EXISTS idx_items_cluster ON items(cluster_id);
CREATE INDEX IF NOT EXISTS idx_items_timestamp ON items(timestamp);
"""


def _apply_migrations(conn: sqlite3.Connection) -> None:
    """Additive column migrations for DBs created before a column existed.
    `CREATE TABLE IF NOT EXISTS` never alters an existing table, so new
    columns on already-created tables must be added explicitly. Each step is
    idempotent (guarded by a PRAGMA check) so init_db is safe to re-run."""
    intro_cols = {row["name"] for row in conn.execute("PRAGMA table_info(trip_intro)")}
    if "outro_text" not in intro_cols:
        conn.execute("ALTER TABLE trip_intro ADD COLUMN outro_text TEXT NOT NULL DEFAULT ''")

    # Light editing (lightbox): per-item photo rotation and clip in/out points.
    item_cols = {row["name"] for row in conn.execute("PRAGMA table_info(items)")}
    if "rotation" not in item_cols:
        conn.execute("ALTER TABLE items ADD COLUMN rotation INTEGER NOT NULL DEFAULT 0")
    if "clip_start" not in item_cols:
        conn.execute("ALTER TABLE items ADD COLUMN clip_start REAL")
    if "clip_end" not in item_cols:
        conn.execute("ALTER TABLE items ADD COLUMN clip_end REAL")
    if "crop" not in item_cols:
        conn.execute("ALTER TABLE items ADD COLUMN crop TEXT")
    # Per-photo place caption (review "Find place"): shown on the photo itself at
    # generate, independent of the moment/cluster place.
    if "place_name" not in item_cols:
        conn.execute("ALTER TABLE items ADD COLUMN place_name TEXT")
    if "place_source" not in item_cols:
        conn.execute("ALTER TABLE items ADD COLUMN place_source TEXT")

    # Per-moment soundtrack cue: a moment may start a new music track, which then
    # plays until the next moment that sets one. NULL/'' = continue the current
    # track. Anchored to the moment (not a timestamp) so it survives re-curation.
    cluster_cols = {row["name"] for row in conn.execute("PRAGMA table_info(clusters)")}
    if "music_track" not in cluster_cols:
        conn.execute("ALTER TABLE clusters ADD COLUMN music_track TEXT")


def init_db(db_path: Path = DEFAULT_DB_PATH) -> None:
    conn = get_connection(db_path)
    try:
        conn.executescript(SCHEMA)
        _apply_migrations(conn)
        conn.commit()
    finally:
        conn.close()


if __name__ == "__main__":
    init_db()
    print(f"Initialized database at {DEFAULT_DB_PATH.resolve()}")
