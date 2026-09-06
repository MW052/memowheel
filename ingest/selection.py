"""Representative-photo selection for cloud place-ID (Premise 1, 12): pick the
photo a cluster sends to the vision API to name the place.

(There used to be a second selection here - a "group-shot" rule that auto-kept
one best photo per moment for the film. That was removed: the film now includes
every photo by default and the user curates which to drop, so nothing
auto-prunes inclusion.)
"""

from dataclasses import dataclass


@dataclass
class ClusterItem:
    id: int
    item_type: str  # 'photo' | 'clip'
    quality_score: float
    face_tags: list[str]  # e.g. [], ['user'], ['wife'], ['user', 'wife']


def select_cloud_id_candidate(items: list[ClusterItem]) -> ClusterItem | None:
    """Highest-quality PHOTO in the cluster with no enrolled face present.
    Clips are never candidates - the cloud vision API rejects video
    formats outright, and clips always have empty face_tags (exempt from
    face logic, Premise 23), which previously made them look "face-free"
    and get wrongly selected. Returns None if no eligible photo exists -
    that cluster routes to manual place tagging instead (Premise 1c, 12)."""
    face_free = [
        item for item in items if item.item_type == "photo" and not item.face_tags
    ]
    if not face_free:
        return None
    return max(face_free, key=lambda item: item.quality_score)
