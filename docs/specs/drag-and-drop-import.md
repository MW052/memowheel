# Spec: Drag-and-drop / upload to import

Status: proposed · Author: Michael Weiss · 2026-08-19

## 1. Problem

Today the only way to bring in a trip is to point the app at a **folder path**
(`Choose trip folder` → `/setup/import` → `run_ingest(folder)`). That's great for
local and mounted drives (incl. Google Drive for Desktop), but it can't help when
the photos aren't a browsable folder on disk:

- **Phone Link** (and similar) let the user *drag photos out* of the phone, but
  expose no folder/API to point at.
- A user has a handful of photos in a **download folder / Google Photos export /
  email** and just wants to throw them at the app.
- The phone connects as **MTP** (no path) — copying to a folder first is the
  workaround, but a drop target removes that step.

A **drag-and-drop / file-upload** import handles all of these with one mechanism,
because it doesn't care where the files came from — Phone Link, Explorer, a
cloud download, anything the OS can drag or a file dialog can select.

This is also the **cheapest, lowest-risk** of the "get photos in" ideas: no COM
(device spec), no OAuth (Google Photos spec). It extends a pattern the app
already uses — `UploadFile` for enrollment photos, music, and itinerary.

## 2. Goals / non-goals

**Goals**
- A drop zone + file/folder chooser on the import step: drag photos/clips (or a
  whole folder) onto the app, or pick them, and import them.
- Reuse the **existing** import pipeline (`run_ingest`) and the **date-range
  filter** unchanged.
- Keep EXIF intact (original bytes are uploaded), so auto GPS place-labels and
  capture dates still work — a real edge over the Google Photos API (which strips
  GPS).
- Works with any drag source (Phone Link, Explorer, cloud downloads).

**Non-goals (this pass)**
- Drag-drop *into a specific moment* in review (the review "add media" flow already
  exists via `add_items_from_path`; a drop variant there is a later follow-up).
- Resumable/chunked uploads for huge libraries (see Risks; Phase 2).
- Deleting the phone's copies or any write-back.

## 3. Key design decision: uploaded files are COPIED and kept

The folder import references the user's files **in place** — `items.path` stores
the original absolute path, and everything downstream (thumbnail `/media`
serving, `generate`) re-reads that path later. **Uploaded files have no
persistent original on disk**, so they must be **saved into an app-managed
location and kept for the life of the project** — not a temp dir we delete after
import (that would break serving and render).

Store (see §13.1): **per-project** — `data/projects/<id>/media/<ts>/…` (flat
`data/imported_media/<ts>/` only as a fallback when no current project), with
`items.path` pointing there. Consequences to accept:
- **Disk use**: dropped media is a *copy* under `data/` (unlike in-place folder
  import). For the packaged app that's next to the exe — fine, but note it.
- **Lifecycle**: these copies belong to the project. Cleanup on project delete is
  a follow-up (today project delete already leaves output/archive alone).

This makes drop-import a "copy-in" import, complementing the "reference-in-place"
folder import. Document when to use which (below).

## 4. UX

On the import step (wizard step 01), alongside the existing `Choose trip folder`:

- A **drop zone**: "Drag photos and clips here — from your phone (Phone Link),
  Explorer, or anywhere." Dragover highlights it; drop collects the files.
- A **"Choose files"** button (`<input type=file multiple accept="image/*,video/*">`)
  for a normal file dialog, and a **"Choose a folder"** button
  (`<input type=file webkitdirectory>`) to select an entire folder's contents in
  the browser (uploads all files, preserving relative subpaths).
- The **date-range fields** (Phase-1 date spec) apply to dropped files too.
- Two progress phases: **Uploading… (n/N files)** then the usual import progress.
- On completion, the same finish summary (imported / skipped-out-of-range /
  undated counts).

Guidance to surface (short helper text): *pick a folder* for photos already on a
drive (incl. Google Drive) — nothing is copied; *drop/upload* for photos coming
from Phone Link, a download, or an ad-hoc selection — these are copied into the
app.

## 5. Backend

- New `POST /setup/import-upload` (multipart), fields:
  `files: list[UploadFile]`, `gap_threshold_minutes`, `date_start`, `date_end`,
  and optional `paths: list[str]` (relative paths for folder drop, to keep
  structure / respect excludes).
  - Reject if an import is already running (same guard as `/setup/import`).
  - Validate dates first (reuse `_parse_date_field`), as `/setup/import` now does.
  - Stream each upload to the per-project store `data/projects/<id>/media/<ts>/<safe_relpath>`
    (§13.1), filtering to `PHOTO_EXTENSIONS | CLIP_EXTENSIONS`, uniquifying
    filename collisions.
  - Kick the **existing** background import job pointing `run_ingest` at that dir,
    passing the dates. Reuse `/setup/import/status` for progress and the same
    counts (`skipped_out_of_range`, `included_undated`).
- Filenames: sanitize (`Path(name).name` / the folder-drop relative path,
  stripped of `..`), never trust client paths for anything but a leaf name.
- The saved dir is **not** deleted (see §3).

## 6. Frontend

- Drop handlers (`dragover`/`dragleave`/`drop`) on the drop zone; for dropped
  **folders**, walk `DataTransferItem.webkitGetAsEntry()` recursively to collect
  files (Chromium/Edge — the packaged app's default browser — support this).
- Build a `FormData` with all files (+ relative paths), POST via `fetch`/XHR with
  an **upload progress** handler for the "Uploading…" phase, then poll
  `/setup/import/status` exactly like the folder flow.
- Reuse the existing date inputs and finish summary.

## 7. Edge cases

- **Huge selection** (thousands of files / GB): one multipart POST may be heavy.
  Phase 1 accepts a single large POST over loopback (fast, local); Phase 2 adds
  batching/chunking if needed. Show a clear "this can take a while" note.
- **Non-media files** in a dropped folder → silently skipped (extension filter),
  as the folder import already does.
- **EXIF/orientation/HEIC**: originals keep EXIF (good). HEIC still isn't in
  `PHOTO_EXTENSIONS` — shared follow-up with the other specs (`pillow-heif`).
- **Duplicate filenames** across a selection → uniquify on save.
- **Disk full** while saving → abort, report, remove the partial dir.
- **Browser folder-drop support**: `webkitdirectory` and `webkitGetAsEntry` are
  well supported in Chromium/Edge; if a browser lacks folder drop, the multi-file
  chooser still works.

## 8. Dependencies & packaging

None new — `python-multipart` (already required) handles uploads; FastAPI
`UploadFile` streams to spooled temp files. No COM, no OAuth. This is the main
reason to prefer this feature over the device/Google-Photos integrations.

## 9. Phasing

- **Phase 1:** drop zone + multi-file chooser + `webkitdirectory` folder chooser →
  `/setup/import-upload` → copy-in → reuse import + date filter + counts.
- **Phase 2:** batched/chunked upload for very large trips; drag-drop **into a
  moment** in review (wraps `add_items_from_path`, copy-in variant); progress
  polish.
- **Phase 3:** HEIC (`pillow-heif`), shared with the other import specs;
  **content-hash dedup across sources** and an optional per-import **time offset**
  for clock-skewed devices (§12).

## 10. Risks

- **Large uploads** in a single POST (mitigated by loopback speed; Phase 2 for
  batching).
- **Disk usage** from copy-in (inherent; documented).
- Browser drag-drop quirks across engines (mitigated: chooser fallback).

## 11. Recommendation

Build this **before** the device (MTP/COM) and Google Photos (OAuth) specs: it's
the lowest-effort, lowest-risk, and most *general* "get photos in" mechanism —
one drop zone serves Phone Link, Explorer, cloud downloads, and ad-hoc files, and
it reuses the existing upload + import + date-filter machinery with **no new
dependencies**. The device and Photos integrations can come later for users who
want a fully in-app pull, but many will be well served by "drag them on."

## 12. Multiple sources per project

Because every import **augments** the current project's catalog and re-clusters by
time (§13.2), a project is inherently **multi-source**: import from phone A, then
phone B, then a Google Drive folder, and they merge into one chronological
timeline. No new concept is needed — "several devices for one project" is just
several imports.

**Preferred mechanism when combining external/removable/cloud sources: copy-in
(drag-and-drop upload), not in-place folder import.** The deciding factor is who
owns the files afterward:
- In-place folder import stores `items.path` *into the source* (e.g. two mounted
  Google Drives, a connected phone). Combine several and the project depends on
  **all of them staying available** — unmount a drive or unplug a phone and those
  items' thumbnails/render break.
- Copy-in lands every source under the **project's own media store** (§13.1), each
  import batch in its **own timestamped subfolder**, so two phones / two Drives
  never collide and the project stays **self-contained and portable**.

Use in-place folder import only for stable, always-mounted local folders where
avoiding the disk copy matters.

Two multi-source hazards (follow-ups, not Phase 1):
- **Cross-source duplicates.** Two phones sharing an album, or a Drive that
  already holds a phone's photos, produce the same shot twice. `INSERT OR IGNORE`
  dedupes by **path**, not content, so both land as duplicates (curated out in
  review today). **Content-hash dedup across sources** is the fix — Phase 3.
- **Clock skew between devices.** Clustering orders by timestamp; a device with a
  wrong clock/timezone clusters and sorts in the wrong place. Recoverable in
  review (reorder / reassign a moment), but worth a per-device time sanity check;
  an optional per-import **time offset** could be a later aid.

## 13. Resolved decisions

1. **Store location — per-project: `data/projects/<id>/media/<ts>/`.** Every
   project already owns a directory (`data/projects/<id>/catalog.db`), so keeping
   copy-in media there makes lifecycle trivial — deleting a project can delete its
   media, which a flat `data/imported_media/` can't do cleanly. Files aren't moved
   on project switch, so stored paths stay valid. Resolve the current project via
   `project_store.current_id()`; if there's somehow no current project at import,
   fall back to a flat `data/imported_media/<ts>/`. Phase 1 stores **absolute**
   paths (matching today's folder import); the "app folder moved → absolute paths
   break" fragility already exists for folder imports and isn't new here —
   relative-path resolution is a separate follow-up.
2. **Augment vs fresh — behave exactly like the folder import.** The folder flow
   clears nothing: `run_ingest` does `INSERT OR IGNORE` into the current catalog
   and re-clusters. Drop-import needs no special mode — it's "the trip" for a
   fresh project and naturally **adds to** an existing one. Bonus: dropping onto an
   existing trip augments it, partially covering the review "add media" case.
3. **Phase-1 cap — ~2,000 files / ~5 GB per drop**, with a friendly
   "drop in smaller batches, or pick a folder instead" hint when exceeded. A
   single multipart POST over loopback handles a normal trip; very large dumps
   risk memory/timeout. Because imports accumulate (decision 2), batching by hand
   works today; **Phase 2 automates client-side batching** to lift the cap.
