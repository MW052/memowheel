"""Shared image loading with EXIF-orientation correction.

Neither cv2.imread nor MoviePy's ImageClip respect the EXIF Orientation
tag - a portrait photo stored with a rotation flag loads sideways unless
corrected explicitly. Used by both ingest (face/quality scoring, cv2/BGR
convention) and generate (MoviePy, RGB convention) so the fix is one place.
"""

import numpy as np
from PIL import Image, ImageOps


def load_corrected_rgb(path: str) -> np.ndarray:
    with Image.open(path) as img:
        img = ImageOps.exif_transpose(img)
        return np.array(img.convert("RGB"))


def load_corrected_bgr(path: str) -> np.ndarray:
    return load_corrected_rgb(path)[:, :, ::-1].copy()


def load_corrected_bgr_with_exif(path: str):
    """Decode a photo ONCE, returning (bgr_ndarray, pil_exif). Ingest needs both
    the corrected pixels (faces + quality) and the EXIF (timestamp + GPS); reading
    them separately meant two full decodes per photo (read_exif's img.load() plus
    load_corrected_rgb), which dominated import time. EXIF is read before
    exif_transpose so the orientation/GPS/datetime tags are still present."""
    with Image.open(path) as img:
        exif = img.getexif()
        corrected = ImageOps.exif_transpose(img)
        rgb = np.array(corrected.convert("RGB"))
    return rgb[:, :, ::-1].copy(), exif


def apply_crop(rgb: np.ndarray, crop: str | None) -> np.ndarray:
    """Crop an RGB array by a normalized "x,y,w,h" string (fractions of the
    array's width/height), as set in the lightbox. Returns the array unchanged
    if crop is empty or malformed; bounds are clamped so a bad value can't crash
    the render."""
    if not crop:
        return rgb
    try:
        x, y, w, h = (float(v) for v in str(crop).split(","))
    except (ValueError, TypeError):
        return rgb
    height, width = rgb.shape[:2]
    x0 = max(0, min(int(round(x * width)), width - 1))
    y0 = max(0, min(int(round(y * height)), height - 1))
    x1 = max(x0 + 1, min(int(round((x + w) * width)), width))
    y1 = max(y0 + 1, min(int(round((y + h) * height)), height))
    return rgb[y0:y1, x0:x1]
