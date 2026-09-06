"""Persistent app settings (survives the New-trip archive).

A flat key/value store in data/settings.json for things a user sets in the Setup
screen: the LLM provider/model/key and the music + output folders. Kept OUT of
the catalog DB on purpose, so "New trip" (which archives catalog.db) doesn't wipe
your configuration. Relative to the project root, matching the rest of the app
(the server and manage.py both run from there).
"""

import json
import os
from pathlib import Path

SETTINGS_PATH = Path("data") / "settings.json"

DEFAULTS = {
    # Cloud place-ID is OFF by default so the app works with no API key at all
    # (every stop routes to manual place typing). Turn on in Setup to auto-detect.
    "auto_place_id": "false",
    # LLM for cloud place-ID. provider: openai | openai_compatible | gemini | anthropic
    "llm_provider": "openai",
    "llm_model": "gpt-5.6-terra",
    "llm_api_key": "",
    "llm_base_url": "",  # openai_compatible only
    # Trip country (e.g. "Japan"). When set, it's sent to the place-ID LLM as
    # geographic context so it can disambiguate similar-looking places. Asked in
    # Setup; required before an auto-place-ID import can start.
    "trip_country": "",
    # Film mood: a one-choice preset (keepsake | modern | lively | custom) that
    # sets video_style + motion_style + photo_duration together in the Setup
    # wizard. "custom" means the user hand-tuned the three controls below.
    "mood_preset": "keepsake",
    # Video look: keepsake (warm cream) | espresso (dark cinematic) | minimal (clean).
    "video_style": "keepsake",
    # Photo motion: fade (dip to black, light) | static (still, fastest) |
    # ken_burns (slow zoom, slowest to render).
    "motion_style": "fade",
    # How long each photo is shown in the video, in seconds (applies to all photos).
    "photo_duration_seconds": "4",
    # Video generation paths.
    "music_dir": str(Path("data") / "music"),
    "output_dir": str(Path("data") / "output"),
    "output_basename": "trip_video",
    # Playback order of music tracks (filenames in music_dir); extras not listed
    # here are appended in filename order.
    "music_order": [],
    # Multi-project library: id of the project currently loaded into the live
    # catalog.db (see project_store.py). Empty = none yet.
    "current_project": "",
}


def _load() -> dict:
    if SETTINGS_PATH.is_file():
        try:
            return json.loads(SETTINGS_PATH.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            return {}
    return {}


def get_setting(key: str, default=None):
    """Returns the stored value, else the caller's `default`, else the built-in
    DEFAULTS. Treats an empty string as unset (our settings never store a
    meaningful empty value)."""
    value = _load().get(key)
    if value is None or value == "":
        return default if default is not None else DEFAULTS.get(key)
    return value


def get_bool(key: str, default: bool = False) -> bool:
    value = get_setting(key, None)
    if value is None:
        return default
    return str(value).strip().lower() in ("1", "true", "yes", "on")


def is_pro() -> bool:
    """True in a 'pro' edition. NOTE: multiprocessor (parallel) final rendering is
    now a STANDARD feature the user chooses per render, so this flag no longer gates
    it. Kept as a reusable switch for any future licensed feature. Set two ways:
      1. env var TRIPVIDEO_EDITION=pro  - baked into a build/launcher; and
      2. settings key "edition"=pro     - a license the user enters later."""
    if os.environ.get("TRIPVIDEO_EDITION", "").strip().lower() == "pro":
        return True
    return (get_setting("edition", "") or "").strip().lower() == "pro"


def set_setting(key: str, value) -> None:
    data = _load()
    data[key] = value
    SETTINGS_PATH.parent.mkdir(parents=True, exist_ok=True)
    SETTINGS_PATH.write_text(
        json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8"
    )


def ordered_music_files(music_dir) -> list:
    """The .mp3 files in `music_dir` as Paths, in the user's saved `music_order`,
    with any not-yet-ordered files appended in filename order. Shared by the
    Setup music list and the video's `_load_music_playlist`."""
    directory = Path(music_dir)
    if not directory.is_dir():
        return []
    by_name = {p.name: p for p in directory.iterdir() if p.suffix.lower() == ".mp3"}
    order = get_setting("music_order", []) or []
    ordered = [by_name[n] for n in order if n in by_name]
    extras = sorted(
        (p for n, p in by_name.items() if n not in order), key=lambda p: p.name.lower()
    )
    return ordered + extras


def all_settings() -> dict:
    """DEFAULTS overlaid with whatever is stored - for rendering the Setup form.
    Never returns the raw key value verbatim to logs; the caller decides."""
    merged = dict(DEFAULTS)
    merged.update(_load())
    return merged
