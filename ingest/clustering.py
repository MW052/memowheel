"""Gap-based time clustering (Premise 1, 8).

A cluster IS a "moment" - the gap-detected location-change boundary is the
same unit group-shot preference logic (selection.py) operates on. Natural
overnight gaps between days already exceed any reasonable threshold, so
this doesn't need to split by day first - it operates on the whole
timestamp-sorted list.
"""

from datetime import datetime

DEFAULT_GAP_THRESHOLD_MINUTES = 30.0


def cluster_by_gap(
    items: list[tuple[int, datetime]],
    gap_threshold_minutes: float = DEFAULT_GAP_THRESHOLD_MINUTES,
) -> dict[int, int]:
    """items: list of (item_id, timestamp), any order.

    Returns {item_id: cluster_index}. Items with no timestamp are not
    clustered here - the caller routes those through the itinerary-fallback
    or manual-tagging path instead (Premise 1c)."""
    sorted_items = sorted(items, key=lambda pair: pair[1])
    assignments: dict[int, int] = {}
    cluster_index = 0
    prev_timestamp: datetime | None = None
    for item_id, timestamp in sorted_items:
        if prev_timestamp is not None:
            gap_minutes = (timestamp - prev_timestamp).total_seconds() / 60.0
            if gap_minutes > gap_threshold_minutes:
                cluster_index += 1
        assignments[item_id] = cluster_index
        prev_timestamp = timestamp
    return assignments


def cluster_trip_day(cluster_timestamps: list[datetime]) -> str:
    """Returns the ISO date (YYYY-MM-DD) a cluster belongs to, taken from
    its earliest item - used to look up the day's itinerary entry."""
    return min(cluster_timestamps).date().isoformat()
