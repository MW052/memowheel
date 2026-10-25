# Packaging Memowheel (zero-install app)

Goal: hand a friend a folder they double-click - no Python, no `pip`, no
terminal. This uses [PyInstaller](https://pyinstaller.org) to bundle Python and
every dependency into `dist/Memowheel/`.

## Build

From the project root, on a Windows machine that already runs the app from
source:

```powershell
powershell -ExecutionPolicy Bypass -File packaging\build.ps1
```

That installs the deps + PyInstaller and runs `packaging/memowheel.spec`,
producing `dist\Memowheel\Memowheel.exe`. Zip the whole `dist\Memowheel` folder
and share it; the recipient unzips and double-clicks `Memowheel.exe`. It runs
**windowless** - no console flashes open. A system-tray icon appears (Open /
View log / Quit) and the browser opens at the setup screen. Because there's no
console, the server's output goes to a log file (under the per-user data dir),
viewable any time from the app's kebab menu -> "View log". User data (catalog,
`settings.json`, music, output, reference photos, logs) is written to a
**per-user folder kept out of the program folder**: `%LOCALAPPDATA%\Memowheel\data`.
So the app folder is pure program files - **updating is just replacing it**, no
data to preserve. (A `portable.txt` beside the exe forces the old behavior -
data in `<app>\data` - for USB/no-trace use.) Still keep the app folder itself
somewhere writable (Desktop, Documents - not `Program Files`).

## How it fits together

- `app_main.py` - frozen entry point; a thin delegate to `tray_app.main()`
  (shared with `py manage.py tray`), which chdir's next to the exe, redirects
  stdout/stderr to the log file, serves, and shows the system-tray icon.
- `paths.py::resource_path` - finds bundled templates/static/fonts via
  `sys._MEIPASS` when frozen, the source tree in dev. `paths.data_dir()` resolves
  the writable user-data root: `%LOCALAPPDATA%\Memowheel\data` when packaged (a
  `portable.txt` marker or dev keeps it at `./data`), and
  `migrate_legacy_data_if_needed()` moves an older next-to-exe `data/` into it on
  first run (called from `tray_app.main()`).
- `packaging/memowheel.spec` - bundles the assets (including the YuNet/SFace
  face models under `models/faces/`) and `collect_all`s the heavy native packages
  (cv2, imageio-ffmpeg, moviepy, reverse_geocoder, ...).

## Known caveats / still to validate on a clean machine

- **This must be validated on a PC with no Python installed.** Building on a dev
  box proves it compiles and runs there; only a clean machine proves "zero
  install". Native packages (opencv, imageio-ffmpeg) are the usual
  source of missing-DLL / missing-data errors that need spec tweaks.
- **ffmpeg / ffprobe:** MoviePy uses `imageio-ffmpeg`, which bundles an `ffmpeg`
  binary (but NOT `ffprobe`), so a separate ffmpeg install is not required. Clip
  import used to shell out to `ffprobe` directly (`ingest/exif.py`,
  `ingest/quality.py`), which fails on a clean box - now fixed: all container
  probing routes through MoviePy's `ffmpeg_parse_infos` (`ingest/media_probe.py`),
  using that one bundled `ffmpeg`. Verified with `ffprobe`/`ffmpeg` stripped from
  PATH. No direct `ffprobe`/`ffmpeg` subprocess calls remain.
- **No face-model download:** the YuNet + SFace ONNX models are bundled in-repo
  under `models/faces/` and picked up by the spec, so the first enroll works fully
  offline (the YuNet + SFace models are bundled, so there is no first-run download).
- **Size:** the unzipped folder is roughly 430 MB (OpenCV and friends dominate);
  zipped for distribution it's about 195 MB. A one-file build is possible but
  slower to start and often flakier with native deps; one-folder is recommended.
- **Antivirus / SmartScreen:** unsigned PyInstaller exes can trigger warnings.
  Code-signing removes this but needs a certificate.
