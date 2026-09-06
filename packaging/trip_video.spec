# PyInstaller spec for Trip Video / Memowheel (one-folder build).
# Build from the project root:  py -m PyInstaller --noconfirm packaging/trip_video.spec
# Produces dist/TripVideo/TripVideo.exe (double-clickable, no Python needed).

import os
from PyInstaller.utils.hooks import collect_all, copy_metadata

# PyInstaller resolves relative script/data paths against the spec's own folder
# (packaging/), not the CWD - so anchor everything to the project root with an
# absolute path. build.ps1 runs pyinstaller with CWD = project root; SPECPATH
# (this spec's folder) is the fallback. Pick whichever actually holds app_main.py.
_candidates = [os.path.abspath(os.getcwd())]
try:
    _candidates.append(os.path.dirname(os.path.abspath(SPECPATH)))
except NameError:
    pass
ROOT = next(
    (p for p in _candidates if os.path.isfile(os.path.join(p, "app_main.py"))),
    _candidates[0],
)
print(f"[spec] project ROOT = {ROOT}")

# Read-only bundled assets (resolved at runtime via paths.resource_path).
datas = [
    (os.path.join(ROOT, "review_app", "templates"), "review_app/templates"),
    (os.path.join(ROOT, "review_app", "static"), "review_app/static"),
    (os.path.join(ROOT, "generate", "fonts"), "generate/fonts"),
    # YuNet + SFace ONNX models (ingest/faces.py resolves them via
    # paths.resource_path); shipped in-repo, no first-run download.
    (os.path.join(ROOT, "models", "faces"), "models/faces"),
]
binaries = []
hiddenimports = [
    "config", "paths", "features",
    "db.schema", "generate.assemble", "generate.pacing",
    "cloud.llm", "cloud.place_id", "cloud.resolve_places",
    "ingest.faces", "ingest.pipeline", "ingest.selection", "ingest.image_utils",
    "ingest.exif", "ingest.quality", "ingest.media_probe",
    "ingest.clustering", "ingest.itinerary",
    "uvicorn", "uvicorn.logging", "uvicorn.loops.auto",
    "uvicorn.protocols.http.auto", "uvicorn.protocols.websockets.auto",
    "uvicorn.lifespan.on",
    "anthropic",
    # API-key keychain (secret_store.py). keyring finds its backend via
    # importlib.metadata entry points; the Windows backend needs win32ctypes.
    "secret_store",
    "keyring.backends.Windows", "keyring.backends.fail",
    "win32ctypes.core", "win32ctypes.pywin32.win32cred",
    # System-tray launcher (app runs windowless; app_main -> tray_app.main).
    # pystray picks its backend at runtime; the Windows one must be bundled.
    "tray_app", "runtime_log",
    "pystray", "pystray._win32",
]

excludes = []

for pkg in [
    "cv2", "imageio_ffmpeg",
    "reverse_geocoder", "moviepy", "bidi", "openai", "fastapi", "starlette",
    "keyring", "pystray",
]:
    try:
        d, b, h = collect_all(pkg)
        datas += d
        binaries += b
        hiddenimports += h
    except Exception as exc:  # a missing optional package shouldn't kill the build
        print(f"[spec] collect_all({pkg!r}) skipped: {exc}")

# Some packages read their own version / discover plugins at import time via
# importlib.metadata, which needs the .dist-info METADATA bundled - collect_all
# does NOT copy that. imageio/__init__.py reads its own version (crashed the
# frozen app with PackageNotFoundError once moviepy was imported), and keyring
# finds its backend via entry points (without the metadata it finds none and
# silently falls back to plaintext key storage). Copy metadata for every package
# that looks itself up or its plugins at runtime.
for pkg in ["imageio", "imageio_ffmpeg", "moviepy", "keyring", "win32ctypes"]:
    try:
        datas += copy_metadata(pkg)
    except Exception as exc:
        print(f"[spec] copy_metadata({pkg!r}) skipped: {exc}")


a = Analysis(
    [os.path.join(ROOT, "app_main.py")],
    pathex=[ROOT],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    runtime_hooks=[],
    excludes=excludes,
    noarchive=False,
)
pyz = PYZ(a.pure)
exe = EXE(
    pyz, a.scripts, [],
    exclude_binaries=True,
    name="TripVideo",
    # Windowless: no console flashes on launch. The app lives in the system tray
    # (Open / View log / Quit) and everything that would have gone to the console
    # is captured to data/logs/tripvideo.log, viewable from the kebab menu.
    console=False,
    disable_windowed_traceback=False,
)
coll = COLLECT(exe, a.binaries, a.datas, name="TripVideo")
