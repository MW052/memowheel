"""Geo lookups for the "Find my trip by place" import mode.

Photos carry GPS in EXIF; this module turns those coordinates into human place
labels ("Lisbon, Lisboa") entirely OFFLINE, so a user can point at a big camera
roll and pull out only the photos taken at a place - no photo, and no
coordinate, ever leaves the machine.

Two directions, both backed by the same `reverse_geocoder` city database that
already ships with the app (used at import to caption GPS photos):

- reverse: coordinate -> "City, Region" (`place_label`, `scan_places`).
- forward: a typed name -> a coordinate (`geocode`), read straight from
  reverse_geocoder's bundled `rg_cities1000.csv`. Lets the user type "Lisbon"
  and have every nearby discovered place selected, even when their own photos
  were reverse-geocoded to finer suburb names.

GPS is read EXIF-only (no pixel decode), the same cheap-scan trick `dates.py`
uses, so scanning thousands of files for their places stays fast.
"""

import csv
import math
import os
from pathlib import Path

from PIL import Image

from ingest.exif import _GPS_IFD_TAG, _parse_gps

# The label used when a file has no GPS at all. A sentinel (not a real "City,
# Region") so it can be a selectable bucket - "No location" - without ever
# colliding with a genuine place name. Chosen from a namespace no label uses.
NO_LOCATION = "::no_location"

# How near a discovered place must be to a typed place to count as "the same
# trip destination". Generous enough to sweep a city plus its suburbs and
# day-trips (which reverse-geocode to their own town names) under one typed
# query, without pulling in the next region.
DEFAULT_RADIUS_KM = 60.0


def media_gps(path: Path) -> tuple[float, float] | None:
    """(lat, lon) from a photo's EXIF, or None. EXIF header only - no pixel
    decode - so dating/placing a whole folder stays cheap. Clips carry no usable
    still-GPS here and always return None (they ride along by date/undated)."""
    try:
        with Image.open(path) as img:
            exif = img.getexif()
            gps_ifd = exif.get_ifd(_GPS_IFD_TAG)
    except Exception:
        return None
    if not gps_ifd:
        return None
    try:
        return _parse_gps(gps_ifd)
    except (KeyError, ValueError, TypeError):
        return None


def _label(result) -> str:
    """A reverse_geocoder hit -> the "City, Region" label. Matches the format
    resolve_places uses so a photo's place here and its caption in review agree."""
    return f"{result['name']}, {result['admin1']}"


def _reverse(coords: list[tuple[float, float]]) -> list[str]:
    """Batch coordinate -> label. mode=1 keeps reverse_geocoder single-threaded
    (import already runs inside the server process; the default mode would spawn
    worker processes there)."""
    import reverse_geocoder
    if not coords:
        return []
    results = reverse_geocoder.search(coords, mode=1, verbose=False)
    return [_label(r) for r in results]


def scan_places(folder: Path, exclude_dirs: set[str] | None = None) -> dict:
    """Read every photo's GPS in `folder` and bucket by reverse-geocoded place,
    so the wizard can show "Lisbon, Lisboa - 214 · Sintra, Lisboa - 61 · No
    location - 240" and let the user tick which places are the trip. Read-only.

    Returns {places: [{label, lat, lon, count}], no_location, total} where
    `places` is sorted by count desc and `lat`/`lon` are a representative point
    for that bucket (used for the typed-place radius match on the client)."""
    from ingest.pipeline import _iter_media_files, PHOTO_EXTENSIONS

    coords: list[tuple[float, float]] = []
    no_location = 0
    total = 0
    for p in _iter_media_files(folder, exclude_dirs):
        total += 1
        gps = media_gps(p) if p.suffix.lower() in PHOTO_EXTENSIONS else None
        if gps is None:
            no_location += 1
        else:
            coords.append(gps)

    labels = _reverse(coords)
    buckets: dict[str, dict] = {}
    for (lat, lon), label in zip(coords, labels):
        b = buckets.get(label)
        if b is None:
            buckets[label] = {"label": label, "lat": lat, "lon": lon, "count": 1}
        else:
            b["count"] += 1

    places = sorted(buckets.values(), key=lambda b: (-b["count"], b["label"]))
    return {"places": places, "no_location": no_location, "total": total}


def labels_for_paths(paths: list[Path]) -> list[str]:
    """Place label for each path, in order, using ONE batch reverse-geocode.
    A path with no GPS (or a clip) gets NO_LOCATION. Used by the import filter to
    decide which files fall in the selected places."""
    from ingest.pipeline import PHOTO_EXTENSIONS

    gps = [media_gps(p) if p.suffix.lower() in PHOTO_EXTENSIONS else None for p in paths]
    have = [(i, g) for i, g in enumerate(gps) if g is not None]
    labels = _reverse([g for _, g in have])
    out = [NO_LOCATION] * len(paths)
    for (i, _g), label in zip(have, labels):
        out[i] = label
    return out


def haversine_km(a: tuple[float, float], b: tuple[float, float]) -> float:
    """Great-circle distance between two (lat, lon) points, in kilometres."""
    r = 6371.0
    lat1, lon1, lat2, lon2 = map(math.radians, (a[0], a[1], b[0], b[1]))
    dlat, dlon = lat2 - lat1, lon2 - lon1
    h = math.sin(dlat / 2) ** 2 + math.cos(lat1) * math.cos(lat2) * math.sin(dlon / 2) ** 2
    return 2 * r * math.asin(math.sqrt(h))


# --- offline forward geocode (typed place name -> coordinate) ---------------

_CITIES: list[tuple[float, float, str]] | None = None  # (lat, lon, "City, Region")


def _load_cities() -> list[tuple[float, float, str]]:
    """Lazily load reverse_geocoder's bundled city table for forward lookups.
    Cached for the process. Same file the reverse path already uses - no new
    dependency, no download, fully offline."""
    global _CITIES
    if _CITIES is not None:
        return _CITIES
    import reverse_geocoder
    csv_path = os.path.join(os.path.dirname(reverse_geocoder.__file__), "rg_cities1000.csv")
    rows: list[tuple[float, float, str]] = []
    try:
        with open(csv_path, encoding="utf-8") as fh:
            for row in csv.DictReader(fh):
                try:
                    rows.append((float(row["lat"]), float(row["lon"]),
                                 f"{row['name']}, {row['admin1']}"))
                except (KeyError, ValueError):
                    continue
    except OSError:
        rows = []
    _CITIES = rows
    return _CITIES


def geocode(query: str) -> dict | None:
    """A typed place name -> {lat, lon, label} from the offline city table, or
    None if nothing matches. Prefers an exact case-insensitive city-name match;
    falls back to the first name that starts with the query. Ambiguous names
    (a "Lisbon" on two continents) resolve to some real Lisbon - harmless,
    because the caller only uses the point to select discovered places NEAR it,
    and a wrong continent simply yields no nearby places."""
    q = (query or "").strip().casefold()
    if not q:
        return None
    exact = None
    prefix = None
    for lat, lon, label in _load_cities():
        name = label.split(",", 1)[0].casefold()
        if name == q:
            exact = (lat, lon, label)
            break
        if prefix is None and name.startswith(q):
            prefix = (lat, lon, label)
    hit = exact or prefix
    if hit is None:
        return None
    return {"lat": hit[0], "lon": hit[1], "label": hit[2]}
