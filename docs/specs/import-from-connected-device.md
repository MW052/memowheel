# Spec: Import from a connected device (Android phones over USB)

Status: v1 defined · Author: Michael Weiss · 2026-08-19

> **Revised 2026-08-29.** The original draft recommended shipping the shell-based
> approach first (Option A below) and deferring WPD, because pulling per-file
> metadata off the device before copying looked too expensive. That premise was
> wrong. Phone media filenames encode the date reliably (`20260711_143022.jpg`,
> `IMG-20260711-WA0001.jpg`, `PXL_20260711_…`, `Screenshot_20260711_…`), and MTP
> hands you the **filename as a cheap property** — no byte transfer. So the app
> can detect the trip's dates *on the device*, cheaply, and copy only the trip.
> That makes **WPD with pre-copy date detection the v1**, not a later phase. The
> filename parser already exists (`ingest/dates.py:_from_filename`) and is reused
> verbatim, so on-device and on-disk dating agree. See §4a. The WPD layer itself is
> implemented as a small **out-of-process C# helper** (`memowheel-device.exe`) over
> a JSON CLI, not in-process COM — for the C# WPD leverage and isolation from flaky
> MTP drivers; `comtypes` remains the one-toolchain fallback. See §4/§4b/§7.

## 1. Problem

A phone plugged in over USB does not appear in the folder picker, so a user can't
import a trip straight from their phone. The "This PC" drive list
(`_windows_drives`, `GetLogicalDrives`) can't help: modern Android phones connect
over **MTP** (Media Transfer Protocol — "File Transfer" mode), and an MTP device
has **no drive letter and no filesystem path**. It lives in the Windows *shell
namespace*; its files move by a device protocol, not by reading a path.

This breaks the importer too, not just the picker: `ingest_phase1` /
`_iter_media_files` walk a `pathlib.Path` and open each file with PIL/ffmpeg
(`media_capture_date`, `_probe_media_file`). None can open an MTP object — there
is no path to open.

So supporting phones is **two problems**: (a) *discover and browse* the device,
and (b) *get the trip's files onto the local filesystem* before the existing
pipeline can touch them — without dragging in the whole camera roll.

## 2. Goals / non-goals

**Goals**
- In the picker, show connected portable devices (Android phones) alongside
  drives; browse into `DCIM` / `Pictures` / `Movies`.
- **Detect the trip's candidate date ranges on the device, from filenames only**
  (cheap — no byte transfer), and let the user pick the trip *before* copying.
- Copy only the selected range's originals to the local project media store, then
  run the **existing** import over that folder — no changes to
  clustering/faces/generate.
- Windows first (the packaged target). Degrade cleanly elsewhere (no devices).

**Non-goals (this pass)**
- Reading MTP files *in place* — MTP has no random-access filesystem; a copy step
  is unavoidable (§3).
- **iPhone / iOS** — iPhones present over PTP with HEIC and album quirks; out of
  scope for v1 (see §7). "Device import" here means **Android/MTP**.
- macOS/Linux device import.
- Writing back to the device (read-only, always).
- Minute-accurate capture times from the device (date-granularity only — enough
  for trip detection; see §4a).

## 3. Why a copy step is mandatory

MTP exposes *objects* (ids + properties), streamed through the Windows Portable
Devices (WPD) API. There is no `open(path)`; a file must be pulled to a real local
file before PIL/ffmpeg can read it. Every design ends in the same place:
**selected objects are copied to a local folder, and the existing folder-import
runs on that folder.** The device work sits entirely in front of the current
pipeline; nothing downstream changes.

## 4. Approach — a small C# device helper, called out-of-process

**Chosen implementation: a standalone `memowheel-device.exe` (C#) that owns all
WPD/COM, invoked by the Python backend over a tiny JSON CLI.** Its whole job is:
enumerate devices, enumerate folders/media (names + dates + sizes, no bytes),
copy requested objects. The rest of Memowheel never touches COM.

Why this over in-process `comtypes`:
- **The language does the heavy lifting.** WPD is a COM API built for C#. An
  MIT-licensed wrapper (`MediaDevices`, or `Windows.Devices.Portable`) turns
  "enumerate + read properties + stream an object" into a few dozen readable
  lines. The `comtypes` equivalent is hand-marshaled COM (`PROPVARIANT`,
  `IPortableDeviceValues`, `IPortableDeviceKeyCollection`) — hundreds of fragile
  lines. The helper **shrinks** the hardest code, not just moves it.
- **Process isolation from flaky MTP drivers.** MTP calls hang/wedge depending on
  the phone and vendor driver. In-process (comtypes) inside a uvicorn worker, a
  wedged call can hang a server thread with no clean escape. Out-of-process, a
  stuck device is a **subprocess timeout + kill** — it can never take down the
  server. For a non-technical user with a random Android, this robustness is worth
  more than one-language purity.
- **Clean COM apartment + testable seam.** The helper owns its own STA/MTA init (a
  footgun inside a threaded Python server); the Python side is "spawn, read JSON,"
  trivially mockable with **zero COM in the Python test path.**
- **It matches a pattern already in the app.** Memowheel already bundles and
  shells out to a native binary — **ffmpeg** (via imageio-ffmpeg, under
  `_internal/`). The device helper is the same shape, so it is not a new *kind* of
  thing to ship (see §7).

Two rules that make the helper actually work (learned, not obvious):
- **C#, not C++.** The entire benefit is the C# WPD wrapper. Raw C++ WPD is as
  painful as comtypes — it throws away the leverage while keeping the pain. Only
  reach for C++ to avoid .NET on principle, and then nothing is gained.
- **Batch the contract — do NOT copy one object per process.** A per-object
  `copy <device> <object> <path>` spawns thousands of processes across a DCIM.
  `list-media` returns the whole array in one call, and `copy` takes a **manifest**
  of objects in one invocation with streamed progress (§4b).

**Fallback (documented, not chosen): in-process WPD via `comtypes`.** If a second
toolchain / signed binary is unwanted, `comtypes` keeps everything one-language at
the cost of the uglier, more fragile COM above and no process isolation. Same
`ingest/device_import.py` seam (§5.4), so this is reversible.

**Rejected: `Shell.Application` (Shell32 `CopyHere`)** — cannot read reliable
per-object metadata before copying (kills the cheap §4a detection) and its copy is
an async native dialog with no clean completion signal. **Rejected: `libmtp`** —
LGPL + Windows build/bundle friction.

### 4b. The device helper's contract (`memowheel-device.exe`)

A stdout-JSON CLI. **UTF-8 output always** (filenames are unicode; the Windows
console codepage will otherwise corrupt them). Non-zero exit + a JSON `{error}` on
failure. Dates are NOT computed here — the helper returns raw filenames and the
Python side runs them through `ingest/dates.py`, so there is one dating
implementation (§4a).

```
memowheel-device.exe list-devices
  -> [{ "id": "...", "name": "Samsung Galaxy S24", "type": "mtp" }]

memowheel-device.exe list-media <device-id> <folder-object-id>
  -> [{ "id": "o12345", "name": "IMG_20260812_103201.jpg",
        "size": 5231142, "modified": "2026-08-12T10:32:01" }]     # names+sizes, no bytes

memowheel-device.exe copy <device-id> --manifest objects.json --dest <dir>
  # copies MANY objects in one run; emits progress lines on stdout, e.g.
  #   {"progress": {"done": 128, "total": 214, "bytes": 734003200}}
  # cancel = the parent kills the child process. Idempotent: skips names already in <dir>.
```

`modified` is the object's WPD date property — used only as a coarse hint; the
authoritative trip date comes from the **filename** via `ingest/dates.py` (§4a).

### 4a. The linchpin: cheap, reliable on-device dates from filenames

Android media filenames encode the date the file landed on the phone, across
sources — verified against real names:

| filename | parsed date | source |
|---|---|---|
| `20260711_143022.jpg` | 2026-07-11 | Samsung camera |
| `IMG-20260711-WA0001.jpg` | 2026-07-11 | WhatsApp image (date only, no time) |
| `VID-20260711-WA0002.mp4` | 2026-07-11 | WhatsApp video |
| `Screenshot_20260711_090500_Chrome.jpg` | 2026-07-11 | screenshot |
| `PXL_20260711_143022123.jpg` | 2026-07-11 | Pixel |
| `random_photo.jpg` | *none* | genuinely date-less |

`WPD_OBJECT_ORIGINAL_FILE_NAME` is a standard WPD object property, read in the
same batch enumeration that lists the objects — **no bytes transferred.** So the
device can be dated by walking object names and running each through the
**existing** `ingest/dates.py:_from_filename` (and `ingest/exif.py`'s
`_FILENAME_TIMESTAMP_RE` for the `YYYYMMDD_HHMMSS` clip form). One source of truth
for "what a filename date means," shared by on-device detection and the on-disk
pipeline — so after copy, the pipeline re-derives the *same* dates (plus real EXIF
where present) and nothing regresses.

This is the same convention the project already trusts for clips (STATUS.md: clips
prefer the filename timestamp over container metadata, "proven reliable").

**Honest caveats (design around them, not blockers):**
- It is a **stored-on-phone** date, not strictly a capture date. For camera shots
  stored ≈ captured; for WhatsApp/received media it is when it hit the phone.
  For *trip detection* that is usually what you want — but never relabel it
  "capture date."
- **Not every app follows the convention.** Names that don't parse are `None` →
  an **"undated"** bucket, shown with a count, handled by the pipeline's existing
  undated-include policy. Never assume 100% coverage.
- **WhatsApp gives date but not time.** Irrelevant here — trip detection is
  date-granularity (matches `date-range-import.md`, which is whole-day).

## 5. Design

### 5.1 Picker: show devices next to drives
`GET /setup/browse` already has a `COMPUTER_ROOT` ("This PC") level returning
`dirs: [{name, path}]`. At `COMPUTER_ROOT`, append **device entries** after the
drives, each with a synthetic path prefix `mtp://<device-id>/<object-id>` and a
📱 icon. `/setup/browse` routes:
- a normal filesystem path → today's `Path` listing;
- an `mtp://…` path → WPD enumeration of that device/object's children.
Device/folder entries are browsable; leaf media isn't listed (the picker selects
folders, as it does now). A locked/unauthorized device still lists, but browsing
into it returns the "unlock + allow file access" hint (§6).

### 5.2 Device flow (the new bit): detect → pick → ranged copy
When the chosen folder is an `mtp://…` path, the wizard runs a **device pass**
before the normal import, driven by the module in §5.4:
1. **Enumerate names+dates** (`enumerate_media`): walk the selected folder's
   objects, read `WPD_OBJECT_ORIGINAL_FILE_NAME`, parse a date via
   `ingest/dates.py`. Cheap — no bytes. Returns `[{object_id, name, date|None}]`.
2. **Cluster into candidate trips:** group the dated objects into contiguous
   date ranges (reuse the day-gap idea; a run of days with a multi-day gap around
   it is a candidate trip). Surface them as pickable ranges — *"Jul 18–27 · 214
   items"* — plus an **"undated"** count.
3. **User picks** a trip (a date range), or falls back to a manual start/end, or
   "copy the whole folder".
4. **Ranged copy** (`copy_objects`): stream only the objects in the chosen range
   (+ undated, per the same include policy) to the local project media store, with
   a real progress callback and cancel. Out-of-range objects are never copied.
5. The **existing** `run_ingest(dest, …)` runs unchanged on the copied folder.
   Because the copy is already date-trimmed, import needs no further date filter —
   though the user can still refine with the existing **find-by-place** mode.

This makes device import a new *source* that feeds a pre-trimmed local folder into
the pipeline. It reuses the date-detection *logic* on-device and the whole import
downstream.

### 5.3 Copy destination — reuse the project media store
Copy into the **per-project media store**, matching the drag-drop upload path
(`_save_uploads` → `project_store.PROJECTS_DIR / <pid> / "media" / <ts>` in
`review_app/routes/setup.py`). Do **not** invent a separate `data/_device_import/`
temp or a bare `originals/`: keeping device copies, dropped files, and their
cleanup under one `<pid>` convention means "new trip"/archive already handles
them. If a distinct "originals" bucket is wanted, make it a sibling folder under
the same `<pid>` dir, not a new top-level path. Files are **kept** (not
temp-deleted): import stores their path and serving/render re-read it later —
same reason `upload-media` keeps its copies.

### 5.4 Isolation
All device-specific code lives in one new Windows-guarded module,
`ingest/device_import.py`, exposing a tiny path-free-to-the-caller interface:
```
list_devices() -> [{id, name}]                       # connected portable devices
list_children(device_path) -> [{name, path, is_folder}]   # browse (mtp:// paths)
enumerate_media(device_path) -> [{object_id, name, date|None}]  # cheap: names+dates, no bytes
copy_objects(device_path, object_ids, dest_dir, progress_cb) -> {copied, bytes}
```
Internally the module **shells out to `memowheel-device.exe`** (located via
`paths.resource_path`, same as the bundled ffmpeg) and parses its JSON (§4b):
`enumerate_media` runs `list-media` then dates each name through
`ingest/dates.py`; `copy_objects` runs `copy --manifest` and translates its
progress lines into `progress_cb`. Date parsing is **not** in this module (nor in
the helper) — one dating implementation, in `ingest/dates.py`. `/setup/browse` and
the device routes call only this interface, so the comtypes fallback is a
drop-in swap, and a build where the helper is absent / non-Windows simply reports
"no devices".

### 5.5 Routes / UI
- `GET /setup/browse` — `mtp://` routing (§5.1).
- `GET /setup/device-scan?path=mtp://…` — returns candidate ranges + undated count
  (§5.2 steps 1–2). Read-only, cheap.
- `POST /setup/device-import` — body: device path + chosen range (or "whole
  folder"). Runs the ranged copy (§5.2 step 4) as a background job with a
  **"Copying from phone…"** progress phase, then chains into the existing import
  job. Reuses the `_import_status` machinery; adds a copy phase ahead of "read".
- Wizard: the device appears as a 📱 entry in the existing folder picker; picking
  it opens a small **"Which trip?"** step (the candidate ranges) before import.
  Bilingual EN+HE strings; RTL-correct.

## 6. Edge cases / failure modes

- **Device asleep / locked / not authorized** → lists, but enumerate/browse
  errors. Android needs "File transfer (MTP)" mode + an on-screen "Allow access"
  tap. Detect and surface that hint; never hang.
- **Slow enumeration** → a 4,000-object `DCIM` is many MTP transactions even for
  names-only. Show progress and a **cancel** on both the scan and the copy.
- **Copy interrupted / device unplugged mid-copy** → abort cleanly, leave the
  partial copy folder for the user or clean it, report a clear error; never leave
  a half-catalog. Prefer a **resumable/idempotent** copy (skip already-copied
  names).
- **Undated objects** → their own count; included by the existing policy (or a
  toggle), never silently dropped.
- **Huge camera roll** → the pre-copy date detection (§4a/§5.2) is what keeps this
  sane; without it a device import would copy everything.
- **Duplicate imports** → `INSERT OR IGNORE` dedupes by `items.path`; the project
  media path is stable per import batch (`<ts>`), so re-importing the same trip on
  a new day duplicates. Acceptable for v1 (user curates); dedupe-by-name/content
  is a later nicety.
- **Disk space** → a large copy needs free space under `data/`; check the chosen
  range's byte total (from object size property) and warn if low.
- **iPhone** → may still appear as a portable device; v1 does not target it. If it
  enumerates, HEIC files won't be in `PHOTO_EXTENSIONS` — skip + count, don't
  crash.

## 7. Dependencies & packaging

The helper changes what gets built, but **adds no step to the end user's
installation** — Memowheel ships as a PyInstaller one-folder build
(`dist/TripVideo/TripVideo.exe` + `_internal/`), run from a launcher, not an MSI.

- **Build-time (developer):** install the **.NET SDK** to build the helper. This
  never reaches the user.
- **Ship it self-contained.** `dotnet publish -r win-x64 --self-contained` (or
  NativeAOT) bakes the runtime **into** `memowheel-device.exe`, so **the user
  installs no .NET runtime** — no "install .NET to continue" popup. Cost is file
  size (~10–70 MB), not an install step. (Never publish framework-dependent — that
  is the path that makes users install a runtime.)
- **Bundle it like ffmpeg.** Drop `memowheel-device.exe` into `_internal/` (add it
  to `packaging/trip_video.spec` `datas`); the app finds it via
  `paths.resource_path`. No COM registration, no user step — WPD is OS-provided.
- **Code-signing.** A native exe that touches USB devices invites SmartScreen /
  AV attention — **but `TripVideo.exe` is already an unsigned native exe with the
  same "unknown publisher" friction today**, so the helper is not a new *category*
  of problem, just one more binary. The helper is launched by the already-running
  app (not double-clicked), so it usually won't raise a *second* SmartScreen
  dialog; the realistic delta is AV heuristics on a device-touching exe. If/when
  the product signs `TripVideo.exe`, **sign the helper with the same cert** in the
  same release step and both are clean.
- **Fallback dep (only if going comtypes instead):** add `comtypes` to
  `requirements.txt` (note: `pywin32-ctypes`, already present, is unrelated) and a
  PyInstaller hook for its generated modules. No second toolchain, no signed
  helper — traded for the in-process COM downsides in §4.
- Windows-only either way; `device_import` no-ops elsewhere.
- HEIC (`pillow-heif`) is a **separate** follow-up, tracked with iPhone support.

## 8. Phasing

- **v1 (this spec):** Android/MTP via `comtypes`+WPD — device in the picker,
  filename-date detection (`enumerate_media` → `ingest/dates.py`), candidate-trip
  pick, ranged copy into the project media store, existing pipeline continues.
  Delivers the core "plug in my phone → get my trip" value with pre-copy trimming.
- **v2:** richer selection (thumbnails, per-file picking), EXIF-over-MTP for exact
  capture times if date-granularity ever proves insufficient, dedupe-by-name.
- **v3:** iPhone/PTP + HEIC (`pillow-heif`).

## 9. Risks

- **Second toolchain + release step.** The helper adds a .NET build and (ideally)
  a signing step to the pipeline. Bounded, but real ops for a solo dev. The
  out-of-process design **removes** the counterpart risk — COM interop inside the
  frozen Python bundle, the classic PyInstaller failure point — so this is a trade,
  not a pure add.
- **The IPC protocol** (§4b) is small but has sharp edges: **UTF-8 stdout** for
  unicode filenames, streamed **progress**, and **cancel = kill child**. Design and
  test these deliberately.
- **Device variability** (Android vendors, locked screens, MTP quirks) needs real
  hardware; can't be verified headlessly. The date/cluster logic is pure and
  hardware-free — unit-test it (§10) independently of the helper.
- **Signing / AV.** A device-touching native exe may draw AV heuristics; mitigated
  by signing under the product cert (§7).

## 10. Verification

- **Hardware-free:** unit-test `enumerate_media`'s consumer — feed sample object
  names (the §4a table + oddballs) through `ingest/dates.py` and the range
  clustering; assert candidate ranges and undated counts. This is most of the
  product logic and needs no phone.
- **On a real Samsung (the reported device):** plug in (File-transfer mode),
  confirm it lists, `device-scan` returns sensible ranges from `DCIM` + WhatsApp
  media, a ranged copy pulls only that range, and the copied folder imports and
  re-dates identically. Restart the server after `.py` edits (no hot reload).
- **On a clean packaged box:** the COM path works frozen (the real risk).

## 11. Resolved decisions

1. **WPD is v1, not a later phase** — the filename-date trick (§4a) makes pre-copy
   detection cheap, which is the whole reason to prefer WPD; the original "Option A
   first" recommendation is superseded.
2. **Implement WPD as an out-of-process C# helper** (`memowheel-device.exe`, §4/§4b),
   not in-process `comtypes` — for the WPD-in-C# leverage and process isolation from
   flaky MTP drivers. `comtypes` is the documented one-toolchain fallback behind the
   same `device_import.py` seam. C#, not C++.
3. **On-device dates come from filenames**, parsed by the existing
   `ingest/dates.py` — no EXIF-over-MTP in v1, date-granularity only.
4. **Copy into the project media store** (`project_store`), not a bare temp or a
   new top-level `originals/` (§5.3).
5. **Android only** for v1; iPhone/HEIC explicitly deferred to v3.
6. Still worth stating: "copy your `DCIM` to a folder first, then import" and the
   drag-from-Phone-Link upload zone both work **today, zero code** — v1's bar is
   being enough better for a non-technical user to justify the helper/COM +
   packaging cost.
