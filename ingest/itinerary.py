"""Itinerary file parser.

Pipe-delimited, one physical line per day, no quoting needed:
    DD/MM/YYYY | short day description | location1, location2, location3 | region

Example:
    11/05/2026 | נחיתה והתמקמות | Tokyo | טוקיו

Deliberately simple: no embedded newlines within a field (that was the
actual cause of the previous CSV-quoting format breaking), and commas
inside the locations field are always safe since `|` is the real
separator, not `,` - no quoting logic needed at all.
"""

from dataclasses import dataclass
from datetime import datetime


class ItineraryFormatError(ValueError):
    pass


@dataclass
class DayItinerary:
    short_info: str       # becomes the day caption seed - "a few words" per day
    locations: list[str]
    region: str


def parse_itinerary(path: str) -> dict[str, DayItinerary]:
    """Returns {iso_date: DayItinerary}. A malformed line fails the whole
    parse immediately, before any ingest processing starts (Premise 17)."""
    result: dict[str, DayItinerary] = {}
    with open(path, "r", encoding="utf-8-sig") as f:
        for line_no, raw_line in enumerate(f, start=1):
            line = raw_line.strip()
            if not line:
                continue  # blank lines are fine, not a format error

            fields = [field.strip() for field in line.split("|")]
            if len(fields) != 4:
                raise ItineraryFormatError(
                    f"{path}:{line_no}: expected 4 fields separated by '|' "
                    f"(date | info | locations | region), got {len(fields)} -> {raw_line!r}"
                )
            date_str, short_info, locations_str, region = fields
            try:
                iso_date = datetime.strptime(date_str, "%d/%m/%Y").date().isoformat()
            except ValueError as e:
                raise ItineraryFormatError(f"{path}:{line_no}: invalid date {date_str!r} ({e})") from e

            locations = [loc.strip() for loc in locations_str.split(",") if loc.strip()]
            if iso_date in result:
                raise ItineraryFormatError(f"{path}:{line_no}: duplicate date {date_str}")
            result[iso_date] = DayItinerary(short_info=short_info, locations=locations, region=region)

    if not result:
        raise ItineraryFormatError(f"{path}: no valid itinerary lines found")
    return result
