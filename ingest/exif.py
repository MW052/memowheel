"""EXIF metadata extraction (GPS + timestamp).

Corrupt/unreadable files raise UnreadableFileError - the pipeline catches
this, skips the file, and logs it (Premise 16) rather than aborting the
whole ingest run.
"""

import os
import re
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from PIL import Image, ExifTags

from ingest.media_probe import probe_creation_time

_FILENAME_TIMESTAMP_RE = re.compile(r"(\d{8})_(\d{6})")

_GPS_IFD_TAG = 0x8825
_DATETIME_ORIGINAL_TAG = 36867
_DATETIME_TAG = 306

_GPS_TAGS = {v: k for k, v in ExifTags.GPSTAGS.items()}


class UnreadableFileError(Exception):
    pass


@dataclass
class ExifResult:
    gps: tuple[float, float] | None  # (lat, lon) or None if absent
    timestamp: str | None  # ISO datetime string, camera-local time (Premise 11)


def _dms_to_decimal(dms, ref) -> float:
    degrees, minutes, seconds = (float(v) for v in dms)
    value = degrees + minutes / 60.0 + seconds / 3600.0
    if ref in ("S", "W"):
        value = -value
    return value


def _parse_gps(gps_ifd: dict) -> tuple[float, float] | None:
    tags = {ExifTags.GPSTAGS.get(k, k): v for k, v in gps_ifd.items()}
    if "GPSLatitude" not in tags or "GPSLongitude" not in tags:
        return None
    lat = _dms_to_decimal(tags["GPSLatitude"], tags.get("GPSLatitudeRef", "N"))
    lon = _dms_to_decimal(tags["GPSLongitude"], tags.get("GPSLongitudeRef", "E"))
    return (lat, lon)


def parse_exif(exif) -> ExifResult:
    """Extract GPS + timestamp from a PIL Exif object (from `img.getexif()`).
    Split out from read_exif so callers that already have the image open - e.g.
    the ingest loader that decodes each photo ONCE - can reuse it without a
    second Image.open/decode."""
    timestamp = None
    raw_dt = exif.get(_DATETIME_ORIGINAL_TAG) or exif.get(_DATETIME_TAG)
    if raw_dt:
        try:
            # EXIF datetime format: "YYYY:MM:DD HH:MM:SS"
            dt = datetime.strptime(raw_dt, "%Y:%m:%d %H:%M:%S")
            timestamp = dt.isoformat()
        except ValueError:
            timestamp = None  # malformed EXIF datetime, not fatal

    gps = None
    try:
        gps_ifd = exif.get_ifd(_GPS_IFD_TAG)
        if gps_ifd:
            gps = _parse_gps(gps_ifd)
    except (KeyError, AttributeError):
        gps = None

    return ExifResult(gps=gps, timestamp=timestamp)


def read_exif(image_path: str) -> ExifResult:
    try:
        with Image.open(image_path) as img:
            img.load()  # force full decode - surfaces truncated/corrupt files here
            return parse_exif(img.getexif())
    except Exception as e:
        raise UnreadableFileError(f"{image_path}: {e}") from e


def read_clip_timestamp(clip_path: str) -> str:
    """Prefers the filename's embedded timestamp (same YYYYMMDD_HHMMSS
    convention as photos) over container metadata - the container's
    creation_time has been observed to be wrong/timezone-garbled after a file has been
    trimmed/edited (off by a few hours, not matching the filename at all),
    while the filename convention has proven reliable across every photo
    in this trip. Falls back to container metadata, then file mtime, only
    when the filename doesn't match that pattern.
    Raises UnreadableFileError if ffmpeg itself can't read the file."""
    filename_match = _FILENAME_TIMESTAMP_RE.search(Path(clip_path).name)
    if filename_match:
        try:
            dt = datetime.strptime(filename_match.group(0), "%Y%m%d_%H%M%S")
            return dt.isoformat()
        except ValueError:
            pass  # doesn't parse as a real date/time, fall through

    try:
        creation_time = probe_creation_time(clip_path)
    except OSError as e:
        raise UnreadableFileError(f"{clip_path}: {e}") from e

    if creation_time:
        # The container's creation_time is often timezone-aware (e.g. trailing
        # "Z"), while photo EXIF timestamps are naive - sorting a mix of aware
        # and naive datetimes crashes. Strip tzinfo WITHOUT converting the
        # wall-clock numbers (no astimezone() call) to stay consistent with
        # Premise 11's existing approach: photos are already treated as naive
        # local time with no real UTC math, so clips get the same treatment
        # rather than a conversion that could shift them relative to photos if
        # the device's reported UTC offset doesn't match assumptions.
        try:
            dt = datetime.fromisoformat(creation_time.replace("Z", "+00:00"))
            return dt.replace(tzinfo=None).isoformat()
        except ValueError:
            pass  # malformed metadata timestamp - fall back to mtime
    return datetime.fromtimestamp(os.path.getmtime(clip_path)).isoformat()
