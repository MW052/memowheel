"""Face gallery search - a shared, maker-agnostic visual-retrieval service.

Enroll a person (the existing `enrolled_faces` flow), then find every photo AND
clip of them across one or more designated **library folders** - thousands of
files. The engine lives here, web-independent and beside `people.py`/`config.py`,
so any maker can import it: Trip Video (import-by-person filter, later) and the
planned Occasion Video (its primary source selection).

How it works (see docs/specs/face-gallery-search.md):
  - **Index once.** Walk the libraries, detect+embed every face into a cached,
    library-global SQLite db (`data/face_index.db`), separate from any trip
    catalog. Photos index one frame; clips are sampled at 1 fps and each detected
    face carries the timestamp (`t_ms`) where it appears.
  - **Search = a vector compare** against the cache (milliseconds), ranked by
    confidence, so searching a second person or a group is instant.
  - **Refresh is a cheap delta-diff** keyed on path+mtime+size; only changed files
    are re-indexed, and files with zero faces are recorded so they're never
    re-scanned.

The stored unit is a generic **subject** (a detection + its embedding + optional
timestamp + a `backend` tag), not "a face in a photo", so clips fold in now and a
future pet backend can register without any schema/query change (designed, not
built - v1 ships only the face backend).

Indexing is sequential here (a resumable background job with a progress bar);
process-pool parallelism is a later optimisation, kept out of v1 to stay robust in
the frozen Windows build.
"""

import os
from pathlib import Path

import numpy as np

import paths
from db.schema import get_connection
from ingest.faces import (
    _DET_SCORE_THRESHOLD,
    _detect,
    _embedding,
    _normalize,
    load_enrollment_set,
)
from ingest.image_utils import load_corrected_bgr
from ingest.pipeline import _iter_media_files, classify_media

INDEX_DB = paths.data_path("face_index.db")

# Detection downscale: faces are found fine on a smaller image and the aligned
# 112x112 crop the embedder uses is unaffected - the single biggest CPU win.
_MAX_DETECT_EDGE = 1024
# Ignore faces smaller than this (original-space px) - tiny background bystanders
# add noise and cost without being searchable.
_MIN_FACE_PX = 40
# Clip sampling: one frame per second, capped so a very long video can't dominate.
_CLIP_FPS_SAMPLE = 1.0
_CLIP_MAX_FRAMES = 240

# Sensitivity tiers -> (confident_cutoff, maybe_cutoff) on SFace cosine similarity.
# Calibrated region from tools/face_diag.py: genuine matches >= ~0.42, impostors
# ~0.26. 'balanced' is the default; the exact values want a calibration pass on a
# real gallery (a build task, not a product decision).
TIERS = {
    "strict":   (0.45, 0.38),
    "balanced": (0.40, 0.30),
    "loose":    (0.36, 0.26),
}
DEFAULT_TIER = "balanced"


SCHEMA = """
CREATE TABLE IF NOT EXISTS media_index (
    path        TEXT PRIMARY KEY,
    mtime       REAL NOT NULL,
    size        INTEGER NOT NULL,
    kind        TEXT NOT NULL,                 -- 'photo' | 'clip'
    subject_cnt INTEGER NOT NULL DEFAULT 0,
    status      TEXT NOT NULL DEFAULT 'ok',    -- 'ok' | 'unreadable'
    indexed_at  TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE TABLE IF NOT EXISTS subjects (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    media_path TEXT NOT NULL REFERENCES media_index(path) ON DELETE CASCADE,
    backend    TEXT NOT NULL DEFAULT 'face',
    bbox       TEXT NOT NULL,
    t_ms       INTEGER,
    det_score  REAL NOT NULL,
    embedding  BLOB NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_subjects_media ON subjects(media_path);
CREATE INDEX IF NOT EXISTS idx_subjects_backend ON subjects(backend);

-- "This isn't them": a photo/clip the user removed from a search target's
-- results. Keyed on the searched target (a person or a group name), so it stays
-- hidden for that target on future searches. A cheap, reversible negative.
CREATE TABLE IF NOT EXISTS match_exclusions (
    target     TEXT NOT NULL,
    media_path TEXT NOT NULL,
    PRIMARY KEY (target, media_path)
);
"""


def init_index_db() -> None:
    conn = get_connection(INDEX_DB)
    try:
        conn.executescript(SCHEMA)
        conn.commit()
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Library folders (shared setting; several allowed)
# ---------------------------------------------------------------------------
def library_dirs() -> list[str]:
    import config
    return [d for d in (config.get_setting("photo_library_dirs", []) or []) if d]


def set_library_dirs(dirs: list[str]) -> None:
    import config
    # de-dup, keep order, drop blanks
    seen, out = set(), []
    for d in dirs:
        d = (d or "").strip()
        if d and d not in seen:
            seen.add(d)
            out.append(d)
    config.set_setting("photo_library_dirs", out)


def add_library_dir(path: str) -> None:
    set_library_dirs(library_dirs() + [path])


def remove_library_dir(path: str) -> None:
    set_library_dirs([d for d in library_dirs() if d != path])


# ---------------------------------------------------------------------------
# Detector/embedder backends (pluggable seam; only 'face' built in v1)
# ---------------------------------------------------------------------------
class SubjectBackend:
    name = "subject"

    def detect_and_embed(self, bgr: np.ndarray):
        """-> list of (bbox (x,y,w,h) in original px, embedding np.ndarray, score)."""
        raise NotImplementedError


class FaceBackend(SubjectBackend):
    """Wraps the existing YuNet detector + SFace embedder (ingest/faces.py)."""
    name = "face"

    def detect_and_embed(self, bgr: np.ndarray):
        small, scale = _downscale(bgr, _MAX_DETECT_EDGE)
        out = []
        for row in _detect(small):
            score = float(row[14])
            if score < _DET_SCORE_THRESHOLD:
                continue
            x, y, w, h = (float(v) / scale for v in row[:4])
            if max(w, h) < _MIN_FACE_PX:
                continue  # too small to be a searchable subject
            emb = _normalize(_embedding(small, row))
            out.append(((x, y, w, h), emb, score))
        return out


_BACKENDS: dict[str, SubjectBackend] = {}


def register_backend(backend: SubjectBackend) -> None:
    _BACKENDS[backend.name] = backend


register_backend(FaceBackend())


def _downscale(bgr: np.ndarray, max_edge: int):
    import cv2
    h, w = bgr.shape[:2]
    m = max(h, w)
    if m <= max_edge:
        return bgr, 1.0
    scale = max_edge / m
    return cv2.resize(bgr, (int(w * scale), int(h * scale)), interpolation=cv2.INTER_AREA), scale


def _fmt_bbox(b) -> str:
    return ",".join(f"{v:.0f}" for v in b)


# ---------------------------------------------------------------------------
# Indexing one file
# ---------------------------------------------------------------------------
def _index_photo(path: str, backend: SubjectBackend):
    bgr = load_corrected_bgr(path)
    return [(bbox, emb, score, None) for (bbox, emb, score) in backend.detect_and_embed(bgr)]


def _index_clip(path: str, backend: SubjectBackend):
    """Sample frames at ~1 fps and detect faces in each, tagging every subject
    with its frame time (t_ms). cv2 returns BGR frames (no EXIF to correct)."""
    import cv2
    cap = cv2.VideoCapture(path)
    if not cap.isOpened():
        raise RuntimeError("cannot open clip")
    try:
        fps = cap.get(cv2.CAP_PROP_FPS) or 0.0
        nframes = cap.get(cv2.CAP_PROP_FRAME_COUNT) or 0.0
        duration = (nframes / fps) if fps > 0 else 0.0  # 0 == unknown
        out = []
        step = 1.0 / _CLIP_FPS_SAMPLE
        t = 0.0
        taken = 0
        while taken < _CLIP_MAX_FRAMES:
            if duration and t > duration:
                break
            cap.set(cv2.CAP_PROP_POS_MSEC, t * 1000.0)
            ok, frame = cap.read()
            if not ok:
                break
            for (bbox, emb, score) in backend.detect_and_embed(frame):
                out.append((bbox, emb, score, int(t * 1000)))
            taken += 1
            t += step
        return out
    finally:
        cap.release()


def _index_one(path: str, kind: str, backend: SubjectBackend):
    if kind == "clip":
        return _index_clip(path, backend)
    return _index_photo(path, backend)


# ---------------------------------------------------------------------------
# Refresh: cheap delta-diff + index the delta (resumable)
# ---------------------------------------------------------------------------
def _under_any(path: str, prefixes: list[str]) -> bool:
    rp = str(Path(path).resolve())
    return any(rp == pre or rp.startswith(pre + os.sep) for pre in prefixes)


def refresh(progress_cb=None, backend_name: str = "face") -> dict:
    """Bring the index up to date with the library folders. Returns a summary
    {indexed, removed, unreadable, total, unreachable:[dirs]}.

    progress_cb(done, total, message) is called per indexed file so the UI can
    show a progress bar. Only the delta (new/changed) is detected+embedded;
    unchanged files (the vast majority) are skipped.
    """
    init_index_db()
    backend = _BACKENDS[backend_name]
    conn = get_connection(INDEX_DB)
    try:
        roots = library_dirs()
        reachable = [r for r in roots if Path(r).is_dir()]
        unreachable = [r for r in roots if not Path(r).is_dir()]
        reachable_prefixes = [str(Path(r).resolve()) for r in reachable]

        # Current media across reachable roots: path -> (mtime, size, kind).
        seen: dict[str, tuple[float, int, str]] = {}
        for r in reachable:
            for p in _iter_media_files(Path(r)):
                try:
                    st = p.stat()
                except OSError:
                    continue
                seen[str(p)] = (st.st_mtime, st.st_size, classify_media(p))

        ledger = {
            row["path"]: (row["mtime"], row["size"])
            for row in conn.execute("SELECT path, mtime, size FROM media_index")
        }

        # Deletions: ledger entries under a REACHABLE root that are gone. Never
        # purge entries under an unreachable root (an unplugged drive is not a
        # mass delete).
        removed = 0
        for path in list(ledger):
            if path in seen:
                continue
            if _under_any(path, reachable_prefixes):
                conn.execute("DELETE FROM media_index WHERE path = ?", (path,))
                removed += 1
        conn.commit()

        # New or changed files (mtime/size differ) need (re)indexing.
        to_index = [
            (path, mt, sz, kind)
            for path, (mt, sz, kind) in seen.items()
            if path not in ledger
            or abs(ledger[path][0] - mt) > 1e-6
            or ledger[path][1] != sz
        ]

        total = len(to_index)
        indexed = unreadable = 0
        for done, (path, mt, sz, kind) in enumerate(to_index):
            if progress_cb:
                progress_cb(done, total, path)
            try:
                subs = _index_one(path, kind, backend)
                status = "ok"
            except Exception:
                subs = []
                status = "unreadable"
                unreadable += 1
            conn.execute("DELETE FROM media_index WHERE path = ?", (path,))  # cascade old subjects
            conn.execute(
                "INSERT INTO media_index (path, mtime, size, kind, subject_cnt, status) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (path, mt, sz, kind, len(subs), status),
            )
            for (bbox, emb, score, t_ms) in subs:
                conn.execute(
                    "INSERT INTO subjects (media_path, backend, bbox, t_ms, det_score, embedding) "
                    "VALUES (?, ?, ?, ?, ?, ?)",
                    (path, backend.name, _fmt_bbox(bbox), t_ms, score,
                     emb.astype(np.float32).tobytes()),
                )
            if status == "ok":
                indexed += 1
            if done % 25 == 0:
                conn.commit()
        conn.commit()
        if progress_cb:
            progress_cb(total, total, "done")
        return {"indexed": indexed, "removed": removed, "unreadable": unreadable,
                "total": len(seen), "unreachable": unreachable}
    finally:
        conn.close()


def index_status() -> dict:
    init_index_db()
    conn = get_connection(INDEX_DB)
    try:
        files = conn.execute("SELECT COUNT(*) AS n FROM media_index").fetchone()["n"]
        photos = conn.execute("SELECT COUNT(*) AS n FROM media_index WHERE kind='photo'").fetchone()["n"]
        clips = conn.execute("SELECT COUNT(*) AS n FROM media_index WHERE kind='clip'").fetchone()["n"]
        subs = conn.execute("SELECT COUNT(*) AS n FROM subjects").fetchone()["n"]
        return {"files": files, "photos": photos, "clips": clips, "subjects": subs}
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Matching (ranked, nearest-of-many embeddings, confidence tiers)
# ---------------------------------------------------------------------------
def all_scores(embeddings: list[np.ndarray],
               backend_name: str = "face") -> list[dict]:
    """Best match score for EVERY indexed media, with NO cutoff applied.

    Returns [{path, kind, score, t_ms}] best-first (raw cosine, un-rounded).
    `match` is this plus a tier cutoff; a calibration pass wants the full
    distribution - every genuine and impostor score - to choose the cutoffs.
    """
    if not embeddings:
        return []
    E = np.stack([e.astype(np.float32) for e in embeddings])  # (k, d)

    init_index_db()
    conn = get_connection(INDEX_DB)
    try:
        kinds = {r["path"]: r["kind"] for r in conn.execute("SELECT path, kind FROM media_index")}
        best: dict[str, tuple[float, int | None]] = {}
        for row in conn.execute(
            "SELECT media_path, t_ms, embedding FROM subjects WHERE backend = ?",
            (backend_name,),
        ):
            v = np.frombuffer(row["embedding"], dtype=np.float32)
            score = float(np.max(E @ v))  # both L2-normalised -> cosine
            p = row["media_path"]
            cur = best.get(p)
            if cur is None or score > cur[0]:
                best[p] = (score, row["t_ms"])

        hits = [
            {"path": p, "kind": kinds.get(p, "photo"), "score": score, "t_ms": t_ms}
            for p, (score, t_ms) in best.items()
        ]
        hits.sort(key=lambda h: h["score"], reverse=True)
        return hits
    finally:
        conn.close()


def match(embeddings: list[np.ndarray], tier: str = DEFAULT_TIER,
          backend_name: str = "face") -> list[dict]:
    """Rank media by how well their faces match ANY of `embeddings`.

    Returns [{path, kind, score, t_ms, tier}] best-first, where `tier` is
    'confident' or 'maybe'. Scores below the tier's maybe-cutoff are dropped.
    """
    hi, lo = TIERS.get(tier, TIERS[DEFAULT_TIER])
    hits = []
    for h in all_scores(embeddings, backend_name):
        score = h["score"]
        if score >= lo:
            hits.append({
                "path": h["path"],
                "kind": h["kind"],
                "score": round(score, 3),
                "t_ms": h["t_ms"],
                "tier": "confident" if score >= hi else "maybe",
            })
    return hits


def _person_embeddings(label: str) -> list[np.ndarray]:
    from db.schema import DEFAULT_DB_PATH
    conn = get_connection(DEFAULT_DB_PATH)
    try:
        return load_enrollment_set(conn).get(label, [])
    finally:
        conn.close()


def _group_member_labels(name: str) -> list[str]:
    from db.schema import DEFAULT_DB_PATH
    conn = get_connection(DEFAULT_DB_PATH)
    try:
        return [
            r["person_label"]
            for r in conn.execute(
                "SELECT m.person_label FROM person_group_members m "
                "JOIN person_groups g ON g.id = m.group_id "
                "WHERE g.name = ? COLLATE NOCASE",
                (name,),
            )
        ]
    finally:
        conn.close()


def find_person(label: str, tier: str = DEFAULT_TIER) -> list[dict]:
    ex = excluded_paths(label)
    return [h for h in match(_person_embeddings(label), tier) if h["path"] not in ex]


def find_group(name: str, tier: str = DEFAULT_TIER) -> list[dict]:
    embs: list[np.ndarray] = []
    for m in _group_member_labels(name):
        embs += _person_embeddings(m)
    ex = excluded_paths(name)
    return [h for h in match(embs, tier) if h["path"] not in ex]


# ---------------------------------------------------------------------------
# "This isn't them" — per-target negative matches
# ---------------------------------------------------------------------------
def exclude_match(target: str, media_path: str) -> None:
    init_index_db()
    conn = get_connection(INDEX_DB)
    try:
        conn.execute(
            "INSERT OR IGNORE INTO match_exclusions (target, media_path) VALUES (?, ?)",
            (target, media_path),
        )
        conn.commit()
    finally:
        conn.close()


def excluded_paths(target: str) -> set[str]:
    init_index_db()
    conn = get_connection(INDEX_DB)
    try:
        return {
            r["media_path"]
            for r in conn.execute(
                "SELECT media_path FROM match_exclusions WHERE target = ?", (target,)
            )
        }
    finally:
        conn.close()
