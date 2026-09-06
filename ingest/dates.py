"""Best-known CAPTURE date for a media file, used by the folder scan and the
import date-range filter.

Order of trust: EXIF DateTimeOriginal/DateTime (photos) or the container's
creation_time (clips) → a YYYYMMDD pattern in the filename → None.

It deliberately STOPS before file mtime. On a Google Drive / OneDrive folder,
mtime is the day the file was *synced to this machine*, not when the photo was
taken - so an mtime-based date would collapse a whole trip onto its download
day and make a date filter exclude the wrong files. A file with no reliable
capture date returns None and is treated as "undated" (the filter includes it).

Photos are read EXIF-only (no pixel decode via `img.load()`), so dating a few
thousand files stays fast; the heavy decode still happens only for the files a
date filter keeps.
"""

import re
from datetime import date, datetime
from pathlib import Path

from PIL import Image

# Kept in sync with ingest.pipeline (PHOTO_EXTENSIONS / CLIP_EXTENSIONS). Defined
# locally so this module stays light - importing pipeline would pull the whole
# decode/face/quality stack into the fast scan path.
PHOTO_EXTENSIONS = {".jpg", ".jpeg", ".png"}
CLIP_EXTENSIONS = {".mp4", ".mov", ".avi"}

_DATETIME_ORIGINAL_TAG = 36867  # EXIF DateTimeOriginal
_DATETIME_TAG = 306             # EXIF DateTime
# YYYYMMDD anywhere in the name, optional -/_ separators (matches 2026-05-11,
# 20260511_120000, IMG_20260511 …). Year constrained to 20xx to avoid matching
# unrelated 8-digit runs.
_FILENAME_DATE_RE = re.compile(r"(20\d{2})[-_]?(\d{2})[-_]?(\d{2})")


def _from_filename(name: str) -> date | None:
    m = _FILENAME_DATE_RE.search(name)
    if m:
        try:
            return date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
        except ValueError:
            pass  # e.g. month 13 / day 32 - not a real date
    return None


def _photo_exif_date(path: Path) -> date | None:
    try:
        with Image.open(path) as img:
            exif = img.getexif()  # header only - no full decode
    except Exception:
        return None  # unreadable/corrupt: let the filename pattern try next
    raw = exif.get(_DATETIME_ORIGINAL_TAG) or exif.get(_DATETIME_TAG)
    if raw:
        try:
            return datetime.strptime(str(raw), "%Y:%m:%d %H:%M:%S").date()
        except ValueError:
            pass  # malformed EXIF datetime, not fatal
    return None


def _clip_container_date(path: Path) -> date | None:
    # Only reached for clips without a filename date. probe_creation_time shells
    # out to the bundled ffmpeg; clips are few relative to photos so this stays
    # cheap. We do NOT fall through to mtime here (see module docstring).
    try:
        from ingest.media_probe import probe_creation_time
        ct = probe_creation_time(str(path))
    except Exception:
        return None
    if ct:
        try:
            return datetime.fromisoformat(ct.replace("Z", "+00:00")).date()
        except ValueError:
            return None
    return None


def media_capture_date(path: Path) -> date | None:
    """The date a photo/clip was taken, or None if nothing reliable is known.
    Never uses file mtime (sync date on cloud folders)."""
    ext = path.suffix.lower()
    if ext in PHOTO_EXTENSIONS:
        return _photo_exif_date(path) or _from_filename(path.name)
    if ext in CLIP_EXTENSIONS:
        # Filename convention is the reliable source for clips (see
        # read_clip_timestamp); fall back to the container's creation_time.
        return _from_filename(path.name) or _clip_container_date(path)
    return _from_filename(path.name)


def in_window(d: date | None, start: date | None, end: date | None) -> bool:
    """Inclusive, whole-day, timezone-agnostic membership test. Undated media
    (d is None) is always included - the filter never drops a file it can't
    confidently place outside the trip."""
    if d is None:
        return True
    if start is not None and d < start:
        return False
    if end is not None and d > end:
        return False
    return True
