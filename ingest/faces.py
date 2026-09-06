"""Face enrollment and tagging using OpenCV's YuNet + SFace (Premise 2, 18, 22).

Face recognition runs entirely locally - embeddings and matching never
leave the machine (Premise 2).

Detection is YuNet (`cv2.FaceDetectorYN`) and recognition is SFace
(`cv2.FaceRecognizerSF`), both from the OpenCV Zoo. Unlike insightface's
`buffalo_l` (whose pretrained weights are non-commercial and a few hundred MB,
downloaded on first run), these two ONNX models are permissively licensed
(YuNet MIT, SFace Apache-2.0), tiny (~230 KB + ~37 MB), and shipped **in the
repo** under `models/faces/` - so there is no first-run download and nothing
here is licence-encumbered. They load through `cv2` (already a dependency),
which is why insightface + onnxruntime were dropped.

Embeddings are 128-dim (SFace), vs. 512-dim for the old buffalo_l model, so any
enrollment saved before this swap is incompatible and the person must be
re-enrolled once (a few reference photos in the setup wizard).
"""

from pathlib import Path

import cv2
import numpy as np

from ingest.image_utils import load_corrected_bgr
from paths import resource_path

# SFace cosine similarity. Calibrated with tools/face_diag.py --calibrate against
# the enrolled reference photos: genuine (same-person) matches sat at >=0.42 and
# impostors (different people) topped out at ~0.26, a clean gap. 0.34 is the
# midpoint - it keeps margin above impostors while leaving headroom below the
# cleanest references, since real travel photos (off-angle, mixed lighting) score
# lower than curated reference shots. Manual correction in the review UI is the
# false-negative backstop (Premise 22). Re-run --calibrate if enrollments change.
MATCH_THRESHOLD = 0.34

# YuNet keeps a detection only above this confidence. Its default (0.9) is strict
# and silently drops small / side-profile / motion-blurred faces before they ever
# reach recognition - the main cause of "it missed faces in the photo". 0.6 finds
# noticeably more faces in group/travel shots; recognition (MATCH_THRESHOLD) then
# still decides who they are, so extra weak detections mostly just go untagged.
_DET_SCORE_THRESHOLD = 0.6

_DETECTOR_MODEL = "models/faces/face_detection_yunet_2023mar.onnx"
_RECOGNIZER_MODEL = "models/faces/face_recognition_sface_2021dec.onnx"

_detector: cv2.FaceDetectorYN | None = None
_recognizer: cv2.FaceRecognizerSF | None = None


def _get_detector() -> cv2.FaceDetectorYN:
    global _detector
    if _detector is None:
        _detector = cv2.FaceDetectorYN.create(
            str(resource_path(_DETECTOR_MODEL)),
            "",
            (320, 320),  # placeholder; reset per-image via setInputSize
            score_threshold=_DET_SCORE_THRESHOLD,
        )
    return _detector


def _get_recognizer() -> cv2.FaceRecognizerSF:
    global _recognizer
    if _recognizer is None:
        _recognizer = cv2.FaceRecognizerSF.create(
            str(resource_path(_RECOGNIZER_MODEL)), ""
        )
    return _recognizer


def _detect(image: np.ndarray) -> np.ndarray:
    """Returns YuNet's face rows for a BGR image: an (N, 15) float32 array where
    each row is [x, y, w, h, 5x(landmark x,y), score]. Empty (0, 15) if none.

    YuNet must be told the exact input size before every detect() call - it
    resizes its network to match, and a stale size silently mis-detects."""
    h, w = image.shape[:2]
    detector = _get_detector()
    detector.setInputSize((w, h))
    _retval, faces = detector.detect(image)
    if faces is None:
        return np.empty((0, 15), dtype=np.float32)
    return faces


def _largest_face(faces: np.ndarray) -> np.ndarray:
    # face[2], face[3] are width and height of the bbox.
    return max(faces, key=lambda f: float(f[2]) * float(f[3]))


def _embedding(image: np.ndarray, face_row: np.ndarray) -> np.ndarray:
    """Aligns and crops the face (SFace expects a canonical 112x112 alignment
    derived from the 5 landmarks in face_row), then returns its 128-d feature."""
    recognizer = _get_recognizer()
    aligned = recognizer.alignCrop(image, face_row)
    feat = recognizer.feature(aligned)  # shape (1, 128)
    return feat[0]


def _normalize(vec: np.ndarray) -> np.ndarray:
    norm = np.linalg.norm(vec)
    result = vec / norm if norm > 0 else vec
    return result.astype(np.float32)


def enroll_face_report(reference_image_paths: list[str]) -> tuple[np.ndarray, int, int]:
    """Like enroll_face, but also reports how many of the supplied photos
    actually contributed a face, so the UI can tell the user "face found in
    3 of 4 photos." Returns (embedding, faces_found, total_photos).

    If a reference photo has multiple detected faces, the largest is used -
    no rejection, no warning (Premise 18, accepted tradeoff: a bad reference
    photo can silently enroll the wrong embedding).
    """
    embeddings = []
    total = len(reference_image_paths)
    for path in reference_image_paths:
        img = load_corrected_bgr(path)
        if img is None:
            continue
        faces = _detect(img)
        if len(faces) == 0:
            continue  # zero-face reference photo silently contributes nothing
        face = _largest_face(faces)
        embeddings.append(_normalize(_embedding(img, face)))
    if not embeddings:
        raise ValueError("No faces detected in any reference photo")
    return _normalize(np.mean(embeddings, axis=0)), len(embeddings), total


def enroll_face(reference_image_paths: list[str]) -> np.ndarray:
    """Returns a single averaged, normalized embedding for one person."""
    embedding, _found, _total = enroll_face_report(reference_image_paths)
    return embedding


# Enrollment quality gate. The "self-consistency" of a person's reference photos
# - how much each photo matches the average of the person's OTHER photos - is the
# same leave-one-out measure tools/face_diag.py --calibrate uses. Healthy
# enrollments sit well above these; a set whose photos disagree (a wrong face, a
# group shot, or two different people) collapses toward impostor levels (~0.26).
ENROLL_ACCEPT_MEDIAN = 0.30  # below this median, reject: the photos don't agree
ENROLL_WEAK_PHOTO = 0.30     # a single photo below this is an odd-one-out


class EnrollmentQuality:
    """Result of assessing a set of reference photos for one person.

    - embedding: the averaged, normalized embedding (usable even if not accepted)
    - photos: list of {"name", "detected": bool, "score": float | None}, where
      score is that photo's leave-one-out self-consistency (None if it was the
      only detected photo, so it can't be cross-checked)
    - found / total: photos with a detectable face / photos supplied
    - median: median self-consistency across detected photos (None if < 2)
    - weak: filenames of detected photos below ENROLL_WEAK_PHOTO (likely wrong
      person / wrong face) - the ones to remove or replace
    - undetected: filenames with no detectable face
    - accepted: whether the enrollment clears the quality gate
    """

    def __init__(self, embedding, photos, median, weak, undetected, accepted):
        self.embedding = embedding
        self.photos = photos
        self.found = sum(1 for p in photos if p["detected"])
        self.total = len(photos)
        self.median = median
        self.weak = weak
        self.undetected = undetected
        self.accepted = accepted

    def as_dict(self) -> dict:
        return {
            "photos": self.photos,
            "found": self.found,
            "total": self.total,
            "median": self.median,
            "weak": self.weak,
            "undetected": self.undetected,
            "accepted": self.accepted,
        }


def assess_enrollment(reference_image_paths: list[str]) -> EnrollmentQuality:
    """Embeds each reference photo, scores how consistent they are with each
    other, and decides whether the set is good enough to enroll. Raises
    ValueError if no photo has a detectable face at all (nothing to enroll).

    The caller shows the score + guidance to the user and only saves the
    embedding when `accepted` is True.
    """
    records = []  # parallel to detected embeddings, keeps name + emb
    photos = []   # user-facing, keeps name + detected + score
    for path in reference_image_paths:
        name = Path(path).name
        img = load_corrected_bgr(path)
        entry = {"name": name, "detected": False, "score": None}
        photos.append(entry)
        if img is None:
            continue
        faces = _detect(img)
        if len(faces) == 0:
            continue
        emb = _normalize(_embedding(img, _largest_face(faces)))
        entry["detected"] = True
        records.append((entry, emb))

    if not records:
        raise ValueError("No faces detected in any reference photo")

    embs = [emb for _entry, emb in records]
    # Leave-one-out self-consistency: each photo vs the mean of the OTHERS, so a
    # bad photo can't flatter itself by being included in its own reference.
    scores = []
    for i, (entry, emb) in enumerate(records):
        rest = [embs[j] for j in range(len(embs)) if j != i]
        if rest:
            other_mean = _normalize(np.mean(rest, axis=0))
            entry["score"] = float(np.dot(emb, other_mean))
            scores.append(entry["score"])
        # single detected photo -> leave score None (can't be cross-checked)

    embedding = _normalize(np.mean(embs, axis=0))
    median = float(np.median(scores)) if scores else None
    weak = [entry["name"] for entry, _emb in records
            if entry["score"] is not None and entry["score"] < ENROLL_WEAK_PHOTO]
    undetected = [p["name"] for p in photos if not p["detected"]]
    accepted = median is None or median >= ENROLL_ACCEPT_MEDIAN
    return EnrollmentQuality(embedding, photos, median, weak, undetected, accepted)


def save_enrollment(conn, person_label: str, embedding: np.ndarray, reference_photo_paths: list[str]) -> None:
    """Persists the enrolled embedding (Premise 6, 10) - local-only, no
    automatic expiry, survives across trips until manually deleted."""
    conn.execute(
        "INSERT INTO enrolled_faces (person_label, embedding, reference_photo_path) VALUES (?, ?, ?)",
        (person_label, embedding.tobytes(), ",".join(reference_photo_paths)),
    )
    conn.commit()


def load_enrolled(conn) -> dict[str, np.ndarray]:
    """Loads the most recent enrollment per person label."""
    rows = conn.execute(
        """SELECT person_label, embedding FROM enrolled_faces e1
           WHERE created_at = (
               SELECT MAX(created_at) FROM enrolled_faces e2
               WHERE e2.person_label = e1.person_label
           )"""
    ).fetchall()
    return {
        row["person_label"]: np.frombuffer(row["embedding"], dtype=np.float32)
        for row in rows
    }


PIXELATION_BLOCKS = 8  # downscale target size - fixed regardless of face size,
                        # which is exactly what makes this scale correctly


def blur_faces(image: np.ndarray) -> np.ndarray:
    """Blurs every detected face's bounding box region - used only for the
    manual "blur & send" fallback (review UI) when a cluster has no
    face-free photo to send to the cloud place-ID API. A deliberate,
    user-triggered exception to the default "never send a photo with an
    identifiable face" rule (Premise 12) - the photo is anonymized first,
    not sent as-is.

    Pixelation (downscale to a small fixed size, then scale back up) rather
    than a fixed-size Gaussian blur alone: a fixed kernel (e.g. 99x99)
    barely touches a large, close-up face since the kernel is small
    relative to the region - pixelating to a small block count scales
    correctly regardless of face size, then an extra blur pass (kernel
    sized relative to the face) smooths pixelation edges for good measure.
    """
    faces = _detect(image)
    blurred = image.copy()
    for face in faces:
        x, y, w, h = (int(v) for v in face[:4])
        x1, y1 = max(x, 0), max(y, 0)
        x2, y2 = min(x + w, image.shape[1]), min(y + h, image.shape[0])
        region = blurred[y1:y2, x1:x2]
        if region.size == 0:
            continue
        rh, rw = region.shape[:2]
        small = cv2.resize(region, (PIXELATION_BLOCKS, PIXELATION_BLOCKS), interpolation=cv2.INTER_LINEAR)
        pixelated = cv2.resize(small, (rw, rh), interpolation=cv2.INTER_NEAREST)
        kernel = max(3, (min(rw, rh) // 2) | 1)  # odd, scales with face size
        blurred[y1:y2, x1:x2] = cv2.GaussianBlur(pixelated, (kernel, kernel), 0)
    return blurred


def tag_faces(image: np.ndarray, enrolled: dict[str, np.ndarray]) -> list[str]:
    """image: a BGR numpy array (as read by cv2.imread).
    enrolled: {'user': embedding, 'wife': embedding}.
    Returns the sorted list of matched person labels present in the image
    (may be empty even if faces were detected, per Premise 22's false-negative
    mitigation via manual correction in the review UI).
    """
    faces = _detect(image)
    present = set()
    for face in faces:
        emb = _normalize(_embedding(image, face))
        for label, ref_emb in enrolled.items():
            similarity = float(np.dot(emb, ref_emb))
            if similarity >= MATCH_THRESHOLD:
                present.add(label)
    return sorted(present)
