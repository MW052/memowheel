# Spec: Import from a connected device (phones / cameras over USB)

Status: proposed · Author: Michael Weiss · 2026-08-19

## 1. Problem

A phone or camera plugged in over USB does not appear in the folder picker, so
a user can't import a trip straight from their phone. The new "This PC" drive
list (`_windows_drives`, `GetLogicalDrives`) can't help: modern phones connect
over **MTP** (Media Transfer Protocol — Android "File Transfer", and how iPhones
expose photos), and an MTP device has **no drive letter and no filesystem path**.
It lives in the Windows *shell namespace* and its files are moved by a device
protocol, not by reading a path.

This also breaks the importer, not just the picker: `ingest_phase1` /
`_iter_media_files` walk a `pathlib.Path` and open each file with PIL and ffmpeg
(`media_capture_date`, `_probe_media_file`). None of those can open an MTP object
— there is no path to open.

So supporting phones is **two problems**: (a) *discover and browse* the device,
and (b) *get its files onto the local filesystem* before the existing pipeline
can touch them.

## 2. Goals / non-goals

**Goals**
- In the picker, show connected portable devices (phones/cameras) alongside
  drives, browse into them, and select a folder (e.g. `DCIM/Camera`).
- Copy the selected media to a local temp folder, then run the **existing**
  import over that folder — no changes to clustering/faces/generate.
- Reuse the date-range filter (Phase-1 date spec) so a whole `DCIM` dump can be
  trimmed to the trip *before* copying, not after.
- Windows first (the packaged target). Degrade cleanly elsewhere.

**Non-goals (this pass)**
- Reading MTP files *in place* (MTP has no random-access filesystem — a copy step
  is unavoidable; see §5).
- iOS deep integration beyond what MTP/WPD exposes (iPhones present photos via
  PTP/MTP; albums and HEIC quirks are out of scope here).
- macOS/Linux device import.
- Two-way sync or writing back to the device (read-only, always).

## 3. Why a copy step is mandatory

MTP exposes *objects* (with ids + properties), streamed through the Windows
Portable Devices (WPD) API. There is no `open(path)`; a file must be pulled to a
real local file before PIL/ffmpeg can read it. Every design below therefore ends
in the same place: **selected objects are copied to a temp folder, and the
existing folder-import runs on that temp folder.** The device work is entirely in
front of the current pipeline; nothing downstream changes.

## 4. Approach — enumeration + copy

Two Windows-native options; pick one in Phase 1.

**Option A — `Shell.Application` (Shell32 COM). RECOMMENDED for Phase 1.**
The same namespace Explorer uses. `Shell.Application` → `NameSpace(17)` ("This
PC") enumerates children *including* MTP devices; `Folder`/`FolderItem` traverse
into the device and its folders; `Folder.CopyHere` copies selected items to a
temp folder. Pros: relatively little code, matches what the user sees in
Explorer, handles the copy for us. Cons: `CopyHere` is asynchronous and shows the
native Windows copy dialog (must poll the temp folder for completion); item
*properties* (capture date, real filename) are limited, so we lean on
`media_capture_date` of the copied files afterward.

**Option B — WPD COM (`IPortableDeviceManager` / `IPortableDevice` / content
enumeration + `IStream`).** Full control: enumerate objects, read
`WPD_OBJECT_ORIGINAL_FILE_NAME` / `WPD_OBJECT_DATE_CREATED`, stream each object
to a local file with our own progress. Pros: real progress, no shell dialog,
metadata before copy (so the date filter can skip files without copying them).
Cons: heavy, verbose COM; more surface to get wrong.

**Recommendation:** ship **Option A** to prove the flow end-to-end, with the
device-access code isolated (see §5.4) behind a small interface so it can be
swapped for **Option B** later if we need pre-copy date filtering or a nicer
progress bar. Dependency: Option A needs `pywin32` (`win32com.client`) or
`comtypes`; neither is a current dependency (`requirements.txt` has the lighter
`pywin32-ctypes`, which is NOT the same). Adding one is part of this feature — a
deliberate call, since it also enlarges the packaged bundle.

## 5. Design

### 5.1 Picker: show devices next to drives
`GET /setup/browse` already has a `COMPUTER_ROOT` ("This PC") level returning
`dirs: [{name, path}]`. Extend it: at `COMPUTER_ROOT`, append **device entries**
after the drives, each with a synthetic path prefix, e.g.
`mtp://<device-id>/<object-id>`. The picker JS treats them like any other entry
(navigates by the entry's `path`). `/setup/browse` learns to route:
- a normal filesystem path → today's `Path` listing;
- an `mtp://…` path → WPD/shell enumeration of that device/object's children.

Device/folder entries are browsable; **leaf media isn't listed** (the picker
selects folders, as it does now). Distinct icon (📱) for a device.

### 5.2 Selecting a device folder → copy → import
`usePickerFolder` posts the chosen path to import as today. `POST /setup/import`
learns that an `mtp://…` folder means: **copy first**.
- New `mtp_copy(device_path, dest_dir, date_start, date_end, progress_cb)` copies
  the folder's media (recursively) into a fresh temp dir under
  `data/_device_import/<timestamp>/`. With Option B, out-of-range files are
  skipped before copying (via object date); with Option A, everything is copied
  and the existing two-pass date filter drops out-of-range files at import.
- Then the **existing** `run_ingest(temp_dir, …, date_start, date_end)` runs
  unchanged. The temp dir is deleted on completion (success or failure).
- The import status/progress UI gains a leading "Copying from device…" phase.

### 5.3 Scan/preview
`GET /setup/scan` (the count + date-range preview) also needs the `mtp://` case.
Option B can report counts/date-range by reading object properties (no copy).
Option A cannot preview without copying, so for Phase-1/Option-A we **skip the
pre-import scan for devices** and show a simpler "Copy from phone, then import"
confirm. (This is the main UX compromise of choosing Option A.)

### 5.4 Isolation
All device-specific code lives in one new module, `ingest/device_import.py`
(Windows-guarded), exposing a tiny interface:
```
list_devices() -> [{id, name}]
list_children(device_path) -> [{name, path, is_folder}]
copy_folder(device_path, dest_dir, date_start, date_end, progress_cb) -> counts
```
`/setup/browse`, `/setup/scan`, `/setup/import` call only this interface, so
Option A→B is a module swap, and non-Windows / no-pywin32 builds simply report
"no devices".

## 6. Edge cases

- **No device / device asleep or locked** → phone shows but browsing errors
  ("Unlock your phone and allow file access"). Android requires the user to grant
  "File transfer" mode + on-screen permission; surface that hint.
- **iPhone** → appears via PTP; may expose only `DCIM` with odd folder names and
  **HEIC** files. HEIC isn't in `PHOTO_EXTENSIONS` — flag as a separate follow-up
  (HEIC decode needs `pillow-heif`).
- **Huge camera roll** → the date filter (pre-copy in Option B, at-import in
  Option A) is what keeps this sane; without it a device import copies everything.
- **Copy interrupted / device unplugged mid-copy** → abort cleanly, delete the
  temp dir, report a clear error; never leave a half-catalog.
- **Disk space** → copying a large roll needs free space in `data/`; check and
  warn if low.
- **Duplicate imports** → `INSERT OR IGNORE` on `items.path` dedupes by the *temp*
  path, which changes each run, so re-importing the same folder duplicates.
  Acceptable for Phase 1 (user curates); note for later.

## 7. Dependencies & packaging

- New dep: `pywin32` (or `comtypes`) for shell/WPD COM. Adds size to the
  PyInstaller bundle and needs a hook so the COM modules are collected. Verify on
  a clean box (native COM + packaging is exactly where frozen apps break).
- Windows-only. `device_import` no-ops on other platforms.

## 8. Phasing

- **Phase 1 (Option A):** device enumeration in the picker + `Shell.Application`
  copy-to-temp + reuse import (with the at-import date filter). Skips device
  scan/preview. Ships the core "import from my phone" value.
- **Phase 2 (Option B):** swap in WPD for pre-copy date filtering, a real copy
  progress bar, and a working scan/preview for devices.
- **Phase 3 (optional):** HEIC support (`pillow-heif`), iPhone niceties,
  dedupe-by-content.

## 9. Risks

- **COM + PyInstaller** is the classic frozen-app failure point; budget clean-box
  shakedown time.
- **Async shell copy** (Option A) has no clean completion signal beyond polling
  the destination — flaky for very large copies; Phase 2 (WPD) removes this.
- **Device variability** (Android vendors, iPhone PTP, locked screens) means real
  testing needs actual hardware; can't be verified headlessly.

## 10. Recommendation

Given the friends-and-family goal, the **lowest-risk high-value** answer is often
"copy photos to a folder first, then import" (already works today, zero code). If
we build in-app device import, do **Phase 1 / Option A** first behind the
`device_import` interface, validate on a real phone + a clean packaged box, and
only reach for WPD (Phase 2) if the copy-dialog UX or the missing device preview
prove worth the extra COM.

## 11. Open questions

1. Is in-app device import worth the `pywin32`/COM dependency + clean-box
   packaging risk, versus documenting "copy to a folder first"?
2. Acceptable for Phase 1 to **skip the scan/preview** for devices (Option A), or
   is the pre-import count/date-range a must-have (forces Option B up front)?
3. HEIC (iPhone) — in scope soon, or explicitly deferred?
