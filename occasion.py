"""Occasion Video core — web-independent, beside `people.py` / `face_index.py`.

Two jobs, both maker-agnostic (the HTTP surface lives in
`review_app/routes/occasion.py`):

1. The single-row **occasion facet** (type / honoree / framing text) — read and
   written like `trip_intro`.
2. **Gather → catalog.** Turn a set of face-search hits for the honoree into the
   project's items, grouped into **calendar-year sections**. Sections are stored
   as `clusters` + `day_captions` — the exact shape Trip Video uses — so the
   shared renderer (`generate/assemble._plan_occasion`) plans them with no schema
   divergence. Years are chronological; undated media goes to a trailing
   "Undated" section the user can place (docs/specs/occasion-video.md).
"""

import json
import sqlite3
from datetime import date
from pathlib import Path

from ingest.dates import media_capture_date

OCCASION_TYPES = ("birthday", "anniversary", "memorial")

# Each occasion type offers THREE video styles (a "mood") the user picks for the
# title cards + messages. Keys index generate.assemble.VIDEO_STYLES; the first of
# each list is the default. Memorial's three are Direction M (DESIGN.md); birthday
# and anniversary get their own warm/elegant trios. Stored in occasion.theme.
OCCASION_STYLES = {
    "birthday":    ["bd_warm", "bd_night", "bd_bright"],
    "anniversary": ["an_classic", "an_romance", "an_gold"],
    "memorial":    ["mem_slate", "mem_evergreen", "mem_candle"],
}


# UI preview swatches (card bg, title text, accent rule) — mirror the colours in
# generate.assemble.VIDEO_STYLES, kept here so the web layer needs no heavy render
# import. If a style's colours change there, update its swatch too.
OCCASION_STYLE_SWATCHES = {
    "bd_warm":      ("#F7F1E6", "#2B2724", "#C0392B"),
    "bd_night":     ("#241F1B", "#F7F1E6", "#C89A3C"),
    "bd_bright":    ("#FAFAF8", "#2B2724", "#C0392B"),
    "an_classic":   ("#F7F1E6", "#2B2724", "#3E5C76"),
    "an_romance":   ("#241F1B", "#F7F1E6", "#B5756A"),
    "an_gold":      ("#F7F1E6", "#2B2724", "#C89A3C"),
    "mem_slate":    ("#241F1B", "#F7F1E6", "#3E5C76"),
    "mem_evergreen": ("#EFEDE8", "#241F1B", "#55685C"),
    "mem_candle":   ("#241F1B", "#F7F1E6", "#B59A62"),
}


# Intro/outro animation choices. 'auto' follows the style's mood; 'none' turns it
# off; the rest force a specific motif (generate/overlays.py).
DECOR_OPTIONS = ("auto", "none", "balloons", "fireworks", "confetti", "hearts")
DEFAULT_DECOR = "auto"


def styles_for(occasion_type: str) -> list[str]:
    return OCCASION_STYLES.get(occasion_type, OCCASION_STYLES["birthday"])


def default_style(occasion_type: str) -> str:
    return styles_for(occasion_type)[0]

# A year section's sort key is "YYYY-01-01" (stored in clusters.trip_day, so the
# renderer's existing ORDER BY trip_day gives chronological sections). Undated
# media sorts after any real year, so its section shows last.
UNDATED_KEY = "9999-12-31"
UNDATED_LABEL = "Undated"


# ---------------------------------------------------------------------------
# Facet
# ---------------------------------------------------------------------------
def get_facet(conn) -> dict:
    row = conn.execute(
        "SELECT occasion_type, honoree, headline, dedication, closing, theme, decor "
        "FROM occasion WHERE id = 1"
    ).fetchone()
    if not row:
        return {"occasion_type": "birthday", "honoree": "", "headline": "",
                "dedication": "", "closing": "", "theme": "", "decor": "auto"}
    return dict(row)


def set_facet(conn, *, occasion_type: str = "birthday", honoree: str = "",
              headline: str = "", dedication: str = "", closing: str = "",
              theme: str = "", decor: str = "auto") -> None:
    if occasion_type not in OCCASION_TYPES:
        occasion_type = "birthday"
    # The theme must be one of this type's styles; fall back to the default.
    if theme not in styles_for(occasion_type):
        theme = default_style(occasion_type)
    if decor not in DECOR_OPTIONS:
        decor = DEFAULT_DECOR
    conn.execute(
        "INSERT INTO occasion (id, occasion_type, honoree, headline, dedication, closing, theme, decor) "
        "VALUES (1, ?, ?, ?, ?, ?, ?, ?) "
        "ON CONFLICT(id) DO UPDATE SET occasion_type=excluded.occasion_type, "
        "honoree=excluded.honoree, headline=excluded.headline, "
        "dedication=excluded.dedication, closing=excluded.closing, theme=excluded.theme, "
        "decor=excluded.decor",
        (occasion_type, honoree, headline, dedication, closing, theme, decor),
    )
    conn.commit()


# ---------------------------------------------------------------------------
# Gather → catalog (year sections)
# ---------------------------------------------------------------------------
COVER_SLOTS = ("intro", "outro")


def set_cover(conn, slot: str, data: bytes | None) -> None:
    """Store (or clear, with data=None) the cover photo for the opening ('intro')
    or closing ('outro') card. Stored as a BLOB in the occasion facet so it
    travels with the project. Assumes the facet row exists (set_facet first)."""
    if slot not in COVER_SLOTS:
        return
    conn.execute("INSERT OR IGNORE INTO occasion (id) VALUES (1)")
    conn.execute(f"UPDATE occasion SET {slot}_cover = ? WHERE id = 1", (data,))
    conn.commit()


def cover_bytes(conn, slot: str) -> bytes | None:
    if slot not in COVER_SLOTS:
        return None
    row = conn.execute(f"SELECT {slot}_cover AS c FROM occasion WHERE id = 1").fetchone()
    return row["c"] if row else None


def has_cover(conn, slot: str) -> bool:
    return cover_bytes(conn, slot) is not None


def _section_key(d: date | None) -> tuple[str, str]:
    """(trip_day sort key, section label) for a capture date."""
    if d is None:
        return UNDATED_KEY, UNDATED_LABEL
    return f"{d.year:04d}-01-01", f"{d.year:04d}"


def build_from_hits(conn, hits: list[dict], tag_label: str | None = None) -> dict:
    """Populate the catalog from face-search hits (face_index MediaHits:
    {path, kind, score, t_ms}). One cluster per calendar year, items ordered
    chronologically within the year, face-tagged with the honoree when it is a
    single person. A clip hit's matching timestamp seeds its trim in-point.

    Returns {items, sections}. Assumes a fresh (empty-of-items) catalog, as a
    newly created occasion project is."""
    # Date each hit once (EXIF reads are not free), then lay them down
    # section-then-chronologically so sort_order is the story order.
    dated = [(h, media_capture_date(Path(h["path"]))) for h in hits]
    dated.sort(key=lambda hd: (_section_key(hd[1])[0],
                               hd[1].isoformat() if hd[1] else ""))

    tag_json = json.dumps([tag_label]) if tag_label else "[]"
    section_cluster: dict[str, int] = {}
    items = 0
    for order, (hit, d) in enumerate(dated):
        key, label = _section_key(d)
        cid = section_cluster.get(key)
        if cid is None:
            cur = conn.execute(
                "INSERT INTO clusters (trip_day, gap_threshold_minutes, place_name, "
                "place_source, consent_status) VALUES (?, 0, NULL, NULL, 'not_needed')",
                (key,),
            )
            cid = cur.lastrowid
            section_cluster[key] = cid
            conn.execute(
                "INSERT OR IGNORE INTO day_captions (trip_day, caption_text) VALUES (?, ?)",
                (key, label),
            )
        kind = hit.get("kind", "photo")
        ts = f"{d.isoformat()}T00:00:00" if d else None
        t_ms = hit.get("t_ms")
        clip_start = (t_ms / 1000.0) if (kind == "clip" and t_ms) else None
        # Triage in the review grid: strong matches are in the film by default,
        # "maybe" matches come in switched off for the user to opt in.
        include = 0 if hit.get("tier") == "maybe" else 1
        try:
            conn.execute(
                "INSERT INTO items (path, item_type, timestamp, cluster_id, face_tags, "
                "include_in_video, sort_order, clip_start) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (hit["path"], kind, ts, cid, tag_json, include, order, clip_start),
            )
            items += 1
        except sqlite3.IntegrityError:
            pass  # items.path is UNIQUE — same file hit twice; keep the first
    conn.commit()
    return {"items": items, "sections": len(section_cluster)}
