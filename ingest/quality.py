"""Quality scoring for stills and clips (Premise 3).

Stills and clips use different criteria - blur/exposure metrics don't
transfer cleanly to video, so clips get their own scoring path entirely.
"""

from dataclasses import dataclass

import cv2
import numpy as np

from ingest.exif import UnreadableFileError
from ingest.media_probe import probe_duration_and_audio

BLUR_VARIANCE_THRESHOLD = 100.0  # Laplacian variance; empirical rule of thumb
GOOD_BRIGHTNESS_RANGE = (30, 225)  # mean grayscale value, out of 255
MIN_CLIP_LENGTH_SECONDS = 2.0
SHAKE_VARIANCE_THRESHOLD = 50.0  # optical-flow magnitude variance; higher = shakier
CLIP_SAMPLE_FRAME_PAIRS = 4


def score_photo_quality(image: np.ndarray) -> float:
    """Returns a 0.0-1.0 quality score. Either blur or bad exposure drags
    the score down (product of two 0-1 sub-scores, not an average) - a
    photo has to pass both checks to score well."""
    gray = cv2.cvtColor(image, cv2.COLOR_BGR2GRAY)

    blur_variance = cv2.Laplacian(gray, cv2.CV_64F).var()
    blur_score = min(blur_variance / BLUR_VARIANCE_THRESHOLD, 1.0)

    mean_brightness = float(gray.mean())
    low, high = GOOD_BRIGHTNESS_RANGE
    if low <= mean_brightness <= high:
        exposure_score = 1.0
    else:
        distance = min(abs(mean_brightness - low), abs(mean_brightness - high))
        exposure_score = max(0.0, 1.0 - distance / 128.0)

    return blur_score * exposure_score


@dataclass
class ClipQualityResult:
    passes: bool
    shake_variance: float
    has_audio: bool
    duration_seconds: float
    reason: str | None  # set when passes is False


def _probe_duration_and_audio(clip_path: str) -> tuple[float, bool]:
    try:
        return probe_duration_and_audio(clip_path)
    except (OSError, KeyError, ValueError) as e:
        # Unreadable / no duration in the container - let the pipeline skip it
        # and log, rather than aborting the whole ingest run (Premise 16).
        raise UnreadableFileError(f"{clip_path}: {e}") from e


def _shake_variance(clip_path: str) -> float:
    cap = cv2.VideoCapture(clip_path)
    frame_count = int(cap.get(cv2.CAP_PROP_FRAME_COUNT))
    if frame_count < CLIP_SAMPLE_FRAME_PAIRS + 1:
        cap.release()
        return 0.0

    sample_indices = np.linspace(0, frame_count - 2, CLIP_SAMPLE_FRAME_PAIRS, dtype=int)
    magnitudes = []
    prev_gray = None
    for idx in sample_indices:
        cap.set(cv2.CAP_PROP_POS_FRAMES, int(idx))
        ok, frame = cap.read()
        if not ok:
            continue
        gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
        if prev_gray is not None:
            flow = cv2.calcOpticalFlowFarneback(
                prev_gray, gray, None, 0.5, 3, 15, 3, 5, 1.2, 0
            )
            magnitude = np.sqrt(flow[..., 0] ** 2 + flow[..., 1] ** 2)
            magnitudes.append(float(magnitude.var()))
        prev_gray = gray
    cap.release()
    return float(np.mean(magnitudes)) if magnitudes else 0.0


def score_clip_quality(clip_path: str) -> ClipQualityResult:
    duration, has_audio = _probe_duration_and_audio(clip_path)

    if duration < MIN_CLIP_LENGTH_SECONDS:
        return ClipQualityResult(
            passes=False, shake_variance=0.0, has_audio=has_audio,
            duration_seconds=duration,
            reason=f"below minimum length ({duration:.1f}s < {MIN_CLIP_LENGTH_SECONDS}s)",
        )

    shake = _shake_variance(clip_path)
    if shake > SHAKE_VARIANCE_THRESHOLD:
        return ClipQualityResult(
            passes=False, shake_variance=shake, has_audio=has_audio,
            duration_seconds=duration,
            reason=f"excessive shake (variance {shake:.1f} > {SHAKE_VARIANCE_THRESHOLD})",
        )

    return ClipQualityResult(
        passes=True, shake_variance=shake, has_audio=has_audio,
        duration_seconds=duration, reason=None,
    )
