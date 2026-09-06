# Memowheel — Trip Video

A locally-run app that turns a trip's photos and short clips into a keepsake
video: it groups them by place and day, lets you review and curate them in a
browser, then renders an MP4 with title cards, place captions, and music. It
runs entirely on your own machine — no accounts, nothing uploaded except the
few photos you approve for AI place-identification.

The app is a small **hub ("Memowheel")** of "makers"; **Trip Video** is the
first and only live one. Occasion Video and Face Morph are stubbed as
coming-soon cards. See `git log --oneline` for the authoritative change log.
All visual decisions (app **and** video) are
governed by `DESIGN.md` — read it before any UI/aesthetic change.

## Two ways to run it

### A. Packaged app (for anyone — no Python, no setup)

A one-folder Windows build produced with PyInstaller. The recipient unzips
`TripVideo` somewhere writable (Desktop, Documents — **not** `Program Files`)
and double-clicks **`TripVideo.exe`**. It runs **windowless** with a
**system-tray icon** (Open / View log / Quit) and opens the browser at the
setup screen. All user data (`data\catalog.db`, `settings.json`, projects,
music, output, reference photos) is written **next to the exe** — so keep the
whole folder together and never overwrite it wholesale on an update.

To build it (on a Windows box that already runs the app from source):

```bash
powershell -ExecutionPolicy Bypass -File packaging\build.ps1
```

That produces `dist\TripVideo\` (also copies the English/Hebrew user guides in).
See `packaging\README.md` for details and clean-machine caveats.

### B. From source (development)

1. `python -m venv .venv && .venv\Scripts\activate` (Windows)
2. `pip install -r requirements.txt`
3. ffmpeg is **not required** — MoviePy uses the `ffmpeg` bundled by
   `imageio-ffmpeg`. (A system ffmpeg on PATH is fine but unused.)
4. **No model download.** Face detection/recognition uses OpenCV's YuNet + SFace
   ONNX models, which ship in the repo under `models/faces/` — no first-run
   download and no internet needed for faces.
5. Run it:

   ```bash
   python manage.py serve
   ```

   The browser opens at http://127.0.0.1:8000 automatically
   (`TRIPVIDEO_NO_BROWSER=1` suppresses it). For a windowless, tray-icon run
   like the packaged app, use `python manage.py tray` instead.

> **No hot reload.** After editing any `.py` file, restart the server — templates
> and CSS reload on their own, Python does not.

## Using the app (GUI)

Everything is drivable from the browser; there is no required CLI step.

1. **Setup wizard (`/setup`)** — a stepped, single-card flow:
   - **Enroll people:** upload a few clear reference photos of each person you
     want recognized. Any number of named people; enrolled faces are **shared
     across all projects**.
   - **Pick a film mood** (Keepsake / Modern / Lively — sets style, motion, and
     per-photo duration together), and configure music, output folder, and the
     location AI in the **Advanced** drawer.
   - **Point at a trip folder** (in-app folder picker) and **import** — a
     background job with a progress bar. Import resolves **no** locations and
     needs no API key.
2. **Review ("Story" workspace)** — moments grouped by day as cover-tiles; open
   one to curate: include/exclude, drag-and-drop reorder within and across
   moments, correct face tags, edit places, set a cover photo, add media from
   this computer, and pick a **per-moment soundtrack**. Every photo starts
   included; you curate *down*. Light editing (rotate, trim a clip, freeform
   crop) lives in the lightbox. Places are filled **on demand** per photo:
   **Find location** tries GPS first (local, free), then the vision AI (needs an
   API key + trip country), and you can always type one manually.
3. **Preview / Finish** — render a **Quick draft** (fast, low-res, watermark-free
   preview) or the **Final film** (full 1080p), watch it in an embedded player,
   and download it. Output lands in `data/output/`.

### Importing from Google Drive or Google Photos

The folder picker can cross drives and connected devices (USB drives, SD-card
readers, mapped network drives — anything Windows gives a drive letter), reached
via the **This PC** level.

- **Google Drive:** install *Google Drive for Desktop* — Drive then mounts as a
  drive letter and appears under **This PC** in the picker, so a Drive folder
  imports like any local one. Set the trip folder to **Available offline** first
  (Drive's default *stream* mode downloads on access, which stalls the import as
  the app decodes each photo).
- **Google Photos:** *not* a filesystem, and Drive for Desktop doesn't include it
  (Google decoupled the two in 2019), so it can't be mounted. Use **Google
  Takeout** (`takeout.google.com`) to export the album/photos, unzip everything
  into one folder, and import that folder. The **date-range filter** on the
  import step trims a large export down to the trip.
- **Phones over USB:** connect the cable and set the phone's USB mode to **File
  transfer** (Android *File transfer / MTP*; not "charging only"). Two ways in:
  - **Directly (simplest):** on the import step click **Choose files**, browse to
    the phone → *Internal storage → DCIM → Camera*, and select the photos. The
    Windows file dialog can reach the phone even though it has no drive letter, so
    the app's own folder picker can't list it — use **Choose files**, not Choose
    trip folder. (MTP-in-dialog behaviour varies by phone/Windows; if selection
    is greyed out, use the fallback.)
  - **Fallback:** in **Explorer**, copy `DCIM\Camera` (or the trip photos) to a
    folder on the PC, then **Choose trip folder**. An **SD-card reader** also
    works directly, since a card gets a drive letter.

  (A future in-app device picker is specced in
  `docs/specs/import-from-connected-device.md`, but the two methods above cover
  phones without it.)

### Multiple projects

The app keeps a **library of projects**. Each keeps its own catalog under
`data/projects/<id>/`; a registry (`data/projects.json`) tracks name, dates, and
whether a film was finished. Switching projects swaps catalogs via SQLite's
online-backup API (safe on Windows). Enrolled people carry across all projects.

## Rendering: single vs. multiprocessor

The final film renders either **single-process** (memory-safe on older or
low-memory machines) or across **all CPU cores** (faster, memory-budgeted worker
planning) — a standard choice the user makes on the Preview page; the pick is
remembered as the default. Draft previews always render as one fast single pass.

## Security model

The server binds **`127.0.0.1:8000` only and has no authentication** — nothing
off the machine can reach it. Two browser-based confused-deputy vectors are
closed at the middleware layer (`review_app/main.py`): the **Host header is
pinned** to `localhost`/`127.0.0.1` (defeats DNS rebinding) and **state-changing
requests must be same-origin** (defeats CSRF).

## Privacy

The app runs **entirely on your machine**. Your photos, clips, and enrolled faces
stay local — face recognition happens on-device and **faces are never uploaded**.

- **The only thing that leaves your computer** is a photo you *explicitly approve*
  for automatic place-naming, which is sent to the location AI you configured
  (OpenAI / Gemini / Claude) using **your own API key**. You can skip this and type
  places by hand; nothing is sent unless you approve it.
- **You are responsible** for the privacy and consent of people who appear in the
  photos you process (relevant under laws such as GDPR and Illinois BIPA).
- The **face models** are OpenCV's YuNet (detection, MIT) and SFace (recognition,
  Apache-2.0) — both permissively licensed and bundled in the repo, so there are
  no non-commercial restrictions and nothing to download.

## CLI (power path)

The GUI is the supported route, but the pipeline is also drivable from
`manage.py`:

```bash
python manage.py enroll <name> <folder-or-photos...>
python manage.py itinerary <itinerary.txt>       # optional day captions
python manage.py ingest <trip-folder> [--exclude sub1,sub2]
python manage.py serve                            # or: tray
```

Maintenance helpers: `reset-ingest` (clear items/clusters, keep enrollment),
`retry-unresolved` (re-queue failed place-ID clusters).

## Bilingual captions

The UI and the generated video are bilingual: **Hebrew (RTL, via `python-bidi`)
+ Latin**. Keep both working in any UI or video change. UI language toggles via
the 🌐 nav control (EN / עברית).

## Project layout

```
db/            SQLite schema (WAL mode, self-migrating on startup)
ingest/        EXIF/GPS, itinerary parsing, face tagging, quality scoring,
               gap clustering, group-shot selection, media probing, orchestration
cloud/         pluggable vision-LLM place-ID (llm.py) + GPS reverse-geocode
review_app/    FastAPI + Jinja2 review UI (127.0.0.1-only), routes/ + templates/
generate/      MoviePy/ffmpeg video assembly (draft + parallel-final modes)
packaging/     PyInstaller spec + build.ps1 (zero-install Windows bundle)
features.py    home-hub maker registry (APP_NAME rebrands the whole app)
project_store  multi-project library (per-project catalogs + shared people)
paths.py       frozen-vs-source asset resolution; writable data/ next to the exe
manage.py      CLI: enroll / itinerary / ingest / serve / tray + maintenance
```

## License

The code is released under the **MIT License** (see `LICENSE`). The bundled
fonts (Assistant, Frank Ruhl Libre, Cutive Mono) are **not** covered by MIT —
they ship under the SIL Open Font License, with their notices kept alongside the
font files (`*-OFL.txt`, `OFL-NOTICE.txt`).

## Known gaps / follow-ups

- **Zero-install not yet proven on a Python-free box:** the build runs and is
  verified on the dev box (enroll, import, windowless launch, logging); a clean
  machine still needs to prove a full **generate** encode. See
  `packaging/README.md`.
- **No automated tests yet** — verification has been manual/live.
- Date-less **photos** (no EXIF) still land in the review's Unassigned section;
  the filename-timestamp fallback exists for clips but not photos.
- Itinerary `locations`/`region` fields are parsed but only `short_info` feeds
  the day caption; they aren't surfaced as one-click place suggestions.
```