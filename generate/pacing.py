"""Duration allocation per item (docs/pacing_design.md).

No beat-sync, no quality-based weighting for v1 - every photo gets the
same base duration, every clip plays for its full natural length. Total
video length is a soft target achieved by how many items you include, not
a hard constraint this module enforces.
"""

DEFAULT_PHOTO_DURATION = 4.0
# Fallback only - used when a clip's real length can't be read. Clips are
# NOT truncated to this; they play to their natural end (a trimmed-looking
# clip in the output means the source file itself was already trimmed).
FALLBACK_CLIP_DURATION = 8.0


def item_duration(
    item_type: str,
    duration_override: float | None,
    clip_natural_duration: float | None = None,
    photo_default: float = DEFAULT_PHOTO_DURATION,
) -> float:
    if duration_override is not None:
        return duration_override
    if item_type == "photo":
        return photo_default
    if clip_natural_duration is None:
        return FALLBACK_CLIP_DURATION
    # Play the whole clip - no cap. The previous MAX_CLIP_DURATION cap was
    # silently trimming clips (video-review feedback).
    return clip_natural_duration
