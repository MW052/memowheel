# Spec: Face gallery search — a shared visual-retrieval service

## Context

Today face recognition is **supervised and trip-scoped**: a user enrolls a person
from a few reference photos (`ingest/faces.py` → `enrolled_faces`, one averaged
128-d SFace embedding per enrollment), and trip *import* tags each imported photo
by matching detected faces against the enrolled set (`tag_faces`, cosine
`MATCH_THRESHOLD = 0.34`). Clips are deliberately exempt (`face_tags = []`).

We want a new capability: **enroll a person, then find every photo (and clip) of
them across a large gallery — thousands of files.** Critically, this is not a
Trip Video feature; it is a **shared service** that the planned **Occasion Video**
maker will depend on as its *primary* ingest (pick a person → gather their
lifetime of photos → montage). So it must be a maker-agnostic core, the way
`people.py` is the shared identities/groups domain.

This mirrors the existing architecture: `people.py` owns *who* exists; this new
core owns *where those people appear across the library*; each maker is a thin UI
on top.

## Goal & scope

**In scope (v1):**
- A **shared `face_index` core** (web-independent, importable by any maker) that
  indexes a photo/video library and answers "give me the media matching this
  person/group, ranked by confidence."
- **Humans, photos AND clips.** Clips reuse the exact face pipeline on sampled
  frames, carrying the timestamp(s) where the person appears.
- **Multi-embedding enrollment + confidence ranking** (needed for lifetime-span
  recall, which Occasion Video depends on).
- A **standalone "Find in library"** UI (reuse the People & Groups results grid).
- **Incremental, resumable index** refreshed by a cheap delta-diff.

**Design-only (not built in v1):**
- **Pets** (dog/cat). Humans use a face backend; pets need a *different*
  detector+embedder and individual pet re-ID is markedly lower accuracy. We design
  a **pluggable backend seam** now so a pet backend can slot in later without
  rework, but build only the face backend.

**Explicitly deferred (hooks called out, not built):**
- Trip Video "import only photos of X" filter (Phase 2).
- Occasion Video source-selection (future maker).

## Principles

- **One shared core, thin maker UIs.** The engine lives in `face_index.py` at the
  project root (beside `people.py` / `config.py`), never inside `setup.py`'s import
  path — that would strand it from Occasion Video.
- **Library-level, global index.** The user designates a photo library once; it is
  indexed once; every maker and every occasion re-queries the same cache for free.
  The index lives in the shared data dir (`paths.data_dir()`), **not** inside any
  trip catalog.
- **The index is a disposable cache**, keyed on `path+mtime`, safe to delete and
  rebuild. Match is always a cheap vector compare, so results are never cached —
  they recompute automatically when photos change *or* enrollment improves.
- **Generic subject unit.** The stored unit is a **detected subject → embedding
  (+ optional timestamp + backend tag)**, not "a face in a photo." That single
  abstraction covers photos + clips now and a pet backend later.
- **All local.** No uploads; consistent with the product's privacy stance.

## Data model (shared data dir, its own SQLite db, e.g. `data/face_index.db`)

Kept separate from trip catalogs because it is library-global, not per-trip.

```sql
-- Ledger: every file we've scanned, INCLUDING ones with zero subjects, so a
-- refresh never re-scans a face-less landscape forever.
CREATE TABLE media_index (
    path        TEXT PRIMARY KEY,
    mtime       REAL NOT NULL,
    size        INTEGER NOT NULL,
    kind        TEXT NOT NULL,           -- 'photo' | 'clip'
    subject_cnt INTEGER NOT NULL DEFAULT 0,
    status      TEXT NOT NULL DEFAULT 'ok',   -- 'ok' | 'unreadable'
    indexed_at  TEXT NOT NULL DEFAULT (datetime('now'))
);

-- One row per detected subject (face today; backend-tagged for the future).
CREATE TABLE subjects (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    media_path TEXT NOT NULL REFERENCES media_index(path) ON DELETE CASCADE,
    backend    TEXT NOT NULL DEFAULT 'face',  -- 'face' now; 'pet' is the seam
    bbox       TEXT NOT NULL,                 -- "x,y,w,h"
    t_ms       INTEGER,                       -- clip frame time; NULL/0 for a photo
    det_score  REAL NOT NULL,
    embedding  BLOB NOT NULL                  -- float32 vector (128-d for face)
);
CREATE INDEX idx_subjects_media ON subjects(media_path);
```

128-d float32 = 512 bytes/subject; 30k subjects ≈ 15 MB. Trivial.

**Library roots** are a shared setting (`config.py` / `settings.json`), e.g.
`photo_library_dirs: [ "<path>", ... ]` — one or more folders the user designates.

**Enrollment change (small):** keep multiple embeddings per person. `enrolled_faces`
already stores one row per enrollment; today `load_enrolled` collapses to the
latest (`MAX(created_at)`). Add `load_enrollment_set(conn) -> {label: [emb, ...]}`
that returns *all* embeddings per label, for nearest-of-many matching. The current
`load_enrolled` stays for the trip tagger (unchanged behaviour).

## The shared core — `face_index.py`

Web-independent; functions take/sense the shared db. Sketch of the surface:

- `library_dirs()` / `set_library_dirs(list)` — the designated roots.
- `refresh(progress_cb=None) -> {indexed, updated, removed, skipped}` — delta-diff
  + index the delta (see below). Resumable.
- `match(embeddings, *, backend="face", tier="balanced") -> [MediaHit]` where a
  `MediaHit` is `{path, kind, score, t_ms?}`, ranked best-first. Core primitive.
- `find_person(conn, label, tier="balanced") -> [MediaHit]` — loads the person's
  embedding set and calls `match`.
- `find_group(conn, name, tier) -> [MediaHit]` — union over members, each hit
  noting which member(s) matched.

### Backend seam (pluggable detector+embedder)

```python
class SubjectBackend:
    name: str                      # 'face', later 'pet'
    def detect_and_embed(self, bgr) -> list[tuple[bbox, np.ndarray, float]]: ...

class FaceBackend(SubjectBackend):  # wraps the existing YuNet + SFace
    name = "face"
    # reuses ingest/faces.py _detect / _embedding / _normalize / _model_buffer
```

A registry maps `name -> backend`. The index stores `backend` per subject; `match`
only compares within one backend. **Pets later** = register a `PetBackend` (its own
detector+embedder) and an enrollment flow; no schema or query changes. *(Design
only in v1 — see "Pets".)*

## Indexing & refresh

### Initial index (the one expensive pass)
Walk each library root (reuse `_iter_media_files` + `classify_media`,
`PHOTO_EXTENSIONS` incl. HEIC). For each file not already current in the ledger:
- **Photo:** load (EXIF-correct, reuse `image_utils`), **downscale long edge to
  ~1024px** (biggest CPU win — detection doesn't need full res), run the face
  backend, store each subject (`t_ms = NULL`).
- **Clip:** sample frames via `cv2.VideoCapture` at ~1 fps (configurable), run the
  backend per sampled frame, store subjects with `t_ms`. Bounded cost: a 30-s clip
  at 1 fps ≈ 30 detections.
- Record the file in `media_index` **even with zero subjects** (`subject_cnt=0`).
- Levers: min face size, per-frame face cap, **process pool** (one model per
  worker — the OpenCV model objects aren't safe to share across threads).
- Background job with a progress bar, reusing the import `_set_status`/poll pattern.

Expectation: ~8–15 photos/sec/core after downscaling → ~10k photos in a few minutes
on multiple cores, **once**.

### Refresh (cheap delta-diff)
Stat-walk the roots, diff against `media_index`:
- **New** (not in ledger) → index.
- **Changed** (`mtime`/`size` differ) → drop its `subjects`, re-index.
- **Deleted** (in ledger, gone) → remove its rows.
- **Unchanged** → skip. (≈99% of the library — the whole point.)

Refresh cost scales with the **delta**, not library size. Resumable: each file is
marked done as processed, so an interrupted refresh resumes on the remainder.

**Triggers:** auto delta-diff when the user enters a flow that reads the index
(library/search screen, or an Occasion Video) — index inline if tiny, offer a
background run if large — plus a manual **"Rescan library"** button. No filesystem
watcher (dependency + daemon complexity not worth it for a local app).

**Safety caveats (build in from the start):**
- **Never purge on an unreachable root.** Archives often live on external/USB/synced
  drives; if a root isn't accessible, **skip the refresh for that root** — don't read
  "drive unplugged" as "10,000 files deleted."
- **Cheap move/rename detection (optional):** a "new" file whose `size+mtime` equals
  a just-"deleted" one is a move → update the path, keep embeddings. Skippable in v1.

## Matching & ranking

- **Score:** for a person with embeddings `E` and a media file with face subjects
  `S`, `score = max over (s in S, e in E) cosine(s.embedding, e)` — nearest of many,
  not average. This is the key recall upgrade for **lifetime spans** (baby → elder),
  which Occasion Video needs.
- **Ranking:** sort media by score desc. Over thousands of varied photos, a hard
  threshold is unreliable, so present **tiers**: *confident* (≥ high cutoff) and a
  separate *maybe* (between low and high) the user confirms/rejects.
- **`tier` / sensitivity:** `strict | balanced | loose` maps to the cutoffs
  (precision ↔ recall), surfaced as a simple control.
- **Clips:** a clip's score is the best over its sampled frames; the hit carries the
  matching `t_ms` range (useful: Occasion Video can auto-pick the segment; Trip Video
  already trims clips).
- **Groups:** union of members' hits, each tagged with which member matched.
- **Freshness:** matching is cheap, so never cache results — recompute so both new
  photos and improved enrollment are reflected automatically.
- *(Optional, later)* confirm/reject on the *maybe* tier appends to the person's
  embedding set — cheap active learning that sharpens the next search.

## UI surfaces (v1: standalone search)

- **Library setting:** designate one or more photo-library folders (reuse the
  existing folder picker `/setup/browse`); show index status + a "Rescan" button.
- **Find in library:** pick a person or group (type-ahead, reuse People & Groups),
  pick sensitivity, run → a **ranked results grid** reusing `people_search.html`
  tiles, with a *maybe* section. Clip tiles show the match timestamp and a ▶ glyph.
- **Actions on results (v1):** **copy or move** the matched files to a folder.
  Nothing else in v1 — "make a trip from these" is deliberately left to Phase 2.

## Maker hooks (called out, not built in v1)

- **Trip Video — import-by-person filter (Phase 2).** `start_import` already filters
  by date and place; add an optional *person/group* filter that runs `match` over the
  source and imports only hits. If the source is the indexed library it's instant;
  an arbitrary folder is indexed on the fly.
- **Occasion Video — source selection (future maker).** Pick person(s) → `find_person`
  over the library → order hits by capture date (`media_capture_date`) → montage.
  The retrieval engine *is* this maker's ingest.

## Pets — design only (not built)

Why separate: YuNet/SFace are human-specific; a dog yields no detections. Realistic
tiers of difficulty with bundleable, CPU-friendly, permissive tooling:
- **"Photos containing a dog/cat"** — easy (a general COCO-class detector in OpenCV's
  dnn). Could ship as a coarse capability.
- **"Photos of *my specific* pet"** — hard; individual pet re-ID with generic
  embeddings is weak (two similar pets collapse). A dedicated pet-reID model is
  harder to source/bundle and still below human-face accuracy.

**Design provision (free to honour now):** the `SubjectBackend` seam + the
`backend` column mean a `PetBackend` (its own detector+embedder + enrollment) slots
in later with no schema/query change. v1 ships only `FaceBackend`; we make no
"find my specific dog" promise at human-face quality.

## Phasing

1. **Shared core + standalone search (the committable deliverable).**
   `face_index.db` schema, `FaceBackend`, index/refresh job (photos + clips),
   multi-embedding `load_enrollment_set`, ranked `match`/`find_person`/`find_group`,
   library setting + "Find in library" UI. i18n EN+HE.
2. **Trip Video import-by-person filter** (reuses the core + `start_import`).
3. **Occasion Video** consumes the core (its own maker spec).
4. **Pet backend** — only if/when pursued; the seam is already in place.

## Performance & privacy

- Index once (~minutes, multi-core, resumable); searches are millisecond vector
  compares. Refresh scales with the delta. Storage ~15 MB per 30k subjects.
- Everything local; no uploads — same privacy posture as the rest of the app.

## Decisions (settled)

1. **Several libraries.** The user may add **one or more** folders; the index unions
   them (each scanned recursively).
2. **Clip sampling: 1 fps** (default). A per-clip frame cap is still a safety knob to
   pick at build time for very long clips.
3. **Sensitivity default: `balanced`.** The three tiers (`strict | balanced | loose`)
   remain; only their **numeric cutoffs** need a calibration pass on real photos
   (like `tools/face_diag.py --calibrate`) — a build task, not a product decision.
4. **Results actions: copy or move only.** No "make a trip from these" in v1 (that
   stays Phase 2).
5. **Separate index db** (`data/face_index.db`), not a table group inside a trip
   catalog — it's a library-global cache.

## Verification approach

- Core unit tests on a throwaway library: index N photos + a clip, confirm subject
  counts, `find_person` ranking, clip `t_ms`, delta-refresh (add/change/delete),
  face-less files recorded once, unreachable-root is skipped not purged.
- A calibration pass to set the sensitivity cutoffs.
- Browser pass on an isolated data dir: designate a library, index with progress,
  search a person, see ranked results incl. a clip tile; re-run after adding a photo
  to confirm the cheap refresh.
