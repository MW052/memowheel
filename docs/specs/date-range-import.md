# Spec: Import photos by trip date range

Status: LANDED (Phase 1) + extended into a find-mode fork · Author: Michael Weiss · 2026-08-17

> **Update 2026-08-29.** Phase 1 shipped as specced (the `media_capture_date`
> helper, the two-pass filter, wizard inputs + itinerary prefill, i18n, CLI
> `--from/--to`). It was then reframed from a filter tucked under the folder step
> into an explicit **Step-1 mode fork** — *I have a trip folder · Find my trip by
> dates · Find my trip by place* — and a **find-by-place** mode was added on the
> same two-pass engine: `ingest/geo.py` reverse-geocodes the folder's own GPS
> tags into "City, Region" buckets (offline, via the bundled `reverse_geocoder`
> dataset — no API, nothing leaves the machine), the user ticks the places (or
> types one, forward-geocoded offline, to sweep it + its suburbs within a
> radius), and `ingest_phase1(places=...)` keeps only the selected labels
> (`geo.NO_LOCATION` = the un-geotagged bucket). Routes: `GET /setup/scan-places`,
> `GET /setup/geocode`. CLI: `ingest --list-places` and `--places "A;B"`.

## 1. Problem

People don't keep a tidy per-trip folder. Their photos live in one big pile —
a phone camera roll, `OneDrive\Pictures\Camera Roll`, `G:\My Drive\...` — mixing
the trip with everything before and after it. Today the app imports *every*
photo/clip under the chosen folder (`ingest_phase1` → `_iter_media_files`), so
pointing it at a camera roll drags in months of unrelated photos and burns
decode/face/quality time on all of them.

Let the user bound the import to the trip's **start and end date**. When an
itinerary is uploaded, its dates supply the range automatically.

## 2. Goals / non-goals

**Goals**
- Optional start/end date bounds on import; out-of-range media is never ingested.
- Filter on the **authoritative capture date** (EXIF / clip timestamp), not file
  mtime — so cloud-synced folders (whose mtime = sync date) filter correctly.
- Prefill the range from the folder scan; let an uploaded itinerary override it.
- Skip work early: don't decode/face/score files outside the window.
- Keep current behavior when no dates are given (full import).

**Non-goals (this pass)**
- Seeding empty day-cards for dates with no photos (possible follow-up).
- Time-of-day boundaries (whole-day, date-only granularity only).
- Filtering media added later in review via "Add from this computer"
  (`add_items_from_path`) — that's an explicit per-file pick, left unfiltered.

## 3. Date source & correctness (the crux)

Two date implementations exist today and **disagree for synced folders**:

| Where | Function | Source |
|---|---|---|
| Scan (wizard preview) | `_guess_date` (`review_app/routes/setup.py:210`) | filename regex → **mtime** |
| Import (stored timestamp) | `_probe_media_file` (`ingest/pipeline.py:44`) | **EXIF `DateTimeOriginal`** (photos) / `read_clip_timestamp` (clips) |

mtime is the copy/sync time, so for Drive/OneDrive a whole trip can look like one
day. **The filter must use the EXIF/clip date** — the same value stored in
`items.timestamp` and used everywhere else.

**Introduce one shared date helper** and use it for both scan and the filter:

```
# ingest/dates.py (new)
def media_capture_date(path: Path) -> date | None:
    """Best-known CAPTURE date. Photos: EXIF DateTimeOriginal/DateTime (read via
    PIL getexif, NO full decode). Clips: read_clip_timestamp. Fallback: filename
    YYYYMMDD pattern. Returns None if nothing reliable (mtime is deliberately NOT
    used — it's the sync date on cloud folders)."""
```

- EXIF-only read (`Image.open(p).getexif()`, tag 0x9003 then 0x0132) is cheap —
  no pixel decode — so scanning thousands of files for their dates is seconds,
  and the expensive face/quality decode still happens only for survivors.
- `_guess_date` in `setup.py` is **replaced** by `media_capture_date`, unifying
  scan and import on one source of truth. (Note: scan gets slightly slower — an
  EXIF read per file instead of an mtime `stat` — acceptable for correctness. If
  a folder proves huge, cap or sample; see Open Questions.)

## 4. UX flow

Wizard "Choose photos" step (`setup.html`), after a folder is scanned:

1. Scan returns `date_start` / `date_end` (now EXIF-based). Show the range and
   **prefill two date inputs** with it. Copy: *"Only include photos taken between
   these dates."* Leaving them at the detected range = import everything (no-op).
2. **Itinerary override.** If an itinerary is uploaded (`POST /setup/itinerary`),
   the response returns `date_start`/`date_end` = min/max of its day keys. The
   client fills the inputs from that and marks them itinerary-sourced (a small
   note: *"Dates from your itinerary."*). User may still edit.
3. On import, the client posts whatever is in the two inputs (may be blank).
4. After import, the finish/summary shows the outcome including any exclusions:
   *"Imported 214 photos and 12 clips · 1,806 photos outside these dates were
   skipped · 63 undated photos were included."*

Undated policy: **include undated media** (no EXIF, no filename date). Rationale:
screenshots/scans/WhatsApp saves legitimately lack dates; they land in review's
Unassigned section where the user already curates. The summary states the count
so it's never silent.

Both inputs are optional and independent: start-only = "from this date on",
end-only = "up to this date".

## 5. Backend changes

### 5.1 `GET /setup/scan`
- Swap `_guess_date` → `media_capture_date`. Response shape unchanged
  (`date_start`, `date_end`).

### 5.2 `POST /setup/itinerary`
- After parsing, return `{"ok": True, "days": n, "date_start": min(keys),
  "date_end": max(keys)}` (keys are already ISO dates). No persistence needed;
  the client carries the range into the import form.

### 5.3 `POST /setup/import`
- Add two optional form fields:
  ```
  date_start: str = Form("")   # ISO YYYY-MM-DD or ""
  date_end:   str = Form("")   # ISO YYYY-MM-DD or ""
  ```
- Parse to `date | None`. Validate: if both set and `start > end` → HTTP 400
  (*"Start date is after the end date."*). Pass through to `run_ingest`.

### 5.4 `run_ingest` / `ingest_phase1` (`ingest/pipeline.py`)
- Thread `date_start: date | None`, `date_end: date | None` through
  `run_ingest` → `ingest_phase1`.
- **Two-pass phase 1** so progress totals and counts are honest:
  1. Cheap pass over `_iter_media_files`: compute `media_capture_date(path)`,
     partition into `in_range` / `out_of_range` / `undated`. `total` = len(
     in_range + undated). Record `skipped_out_of_range = len(out_of_range)`.
  2. Heavy pass (existing decode/tag/score/insert) over `in_range + undated`
     only. Reuse the date already computed (pass it into `_probe_media_file` to
     avoid re-reading EXIF).
- Window predicate (inclusive, date-only, tz-agnostic):
  ```
  def _in_window(d, start, end):
      if d is None: return True            # undated: include (policy)
      if start and d < start: return False
      if end and d > end: return False
      return True
  ```
- Out-of-range files are **not** inserted and **not** written to `skipped_files`
  (that table is for corrupt/unreadable — don't pollute it). The count is
  returned via the import status/summary instead.

### 5.5 Import status/summary
- Extend the status dict (polled at `/setup/import/status`) with
  `skipped_out_of_range` and `included_undated` so the finish card can render the
  sentence in §4.4.

### 5.6 CLI parity (small, optional)
- `manage.py ingest <folder> [--from YYYY-MM-DD] [--to YYYY-MM-DD]` mapping to the
  same `run_ingest` params, so the power path matches the GUI.

## 6. Precedence

Effective range resolution (client-side, then honored verbatim by import):
1. **Itinerary dates** if an itinerary was uploaded (fills + notes the inputs).
2. Else **user-entered** values.
3. Else **scan-detected** range as the prefilled default.
4. Import applies exactly what's in the two form fields (blank = unbounded on
   that side). The server does not re-derive; the client is the single place
   precedence is decided.

## 7. Edge cases

- **Only one bound set** → open-ended on the other side (supported).
- **No bounds** → no filtering (identical to today).
- **start > end** → 400 with a friendly message (no silent empty import).
- **Everything filtered out** (0 in-range) → import completes with a clear
  "0 photos matched these dates — check the range" state, catalog left empty
  (not an error).
- **Undated media** → included, counted (§4).
- **mtime-only files on a synced folder** → correctly treated as undated (not
  mis-dated to the sync day), because `media_capture_date` never uses mtime.
- **`--exclude` subfolders** → orthogonal; both filters apply.
- **Clips** → filtered by `read_clip_timestamp` date, same as photos.
- **Manual add in review** (`add_items_from_path`) → unaffected by design.

## 8. i18n & design

- New EN + HE strings (`review_app/i18n.py`): field labels, helper text, the
  itinerary-source note, and the post-import summary sentence (with pluralized
  counts). Bilingual is mandatory (`CLAUDE.md`).
- Date inputs styled to the wizard card per `DESIGN.md` — read it before adding
  the controls; match the existing field/label styling, warm palette, focus
  ring. RTL: inputs and helper text must lay out correctly in Hebrew.

## 9. Testing / verification

No automated suite in-repo; follow the project's live-verify norm:
- **Unit-check** `_in_window` (both bounds, one bound, none, undated) and
  `media_capture_date` against a photo with EXIF, a filename-dated clip, and a
  date-less file.
- **Live**, in the in-app browser, on a real folder padded with a few out-of-range
  photos: confirm prefill from scan, itinerary override, the skipped/undated
  counts, and that out-of-range files never appear in review. Restart the server
  after the `.py` edits (no hot reload).

## 10. Rollout / phasing

- **Phase 1 (this spec):** `media_capture_date` helper + unify scan; import date
  fields + two-pass filter + counts; wizard inputs, prefill, itinerary override;
  i18n; CLI flags. Ships the whole value.
- **Phase 2 (later, optional):** seed empty day-cards across the range so gaps are
  visible; sample/cap EXIF scanning for very large folders if it proves slow.

## 11. Resolved decisions

1. **Undated media — include + show count.** Date-less files (no EXIF, no
   filename date) are imported and land in review's Unassigned section; the
   post-import summary states how many. (§4, §7)
2. **Itinerary — prefill + editable, with a reset.** An uploaded itinerary fills
   the date inputs but leaves them editable; a small "reset to itinerary dates"
   affordance restores them after any manual edit. Inputs are never locked
   read-only. (§4.2, §6)
3. **Scan cost on very large folders — accept the EXIF-read pass for now.** Ship
   the per-file EXIF date read in scan; revisit sampling/capping only if a real
   large folder (10k+) proves slow. Deferred to Phase 2. (§3, §10)
