"""Procedural, asset-free decorations for Occasion Video intro/outro title cards.

Balloons, hearts, confetti and fireworks are drawn per frame with PIL (numpy +
PIL only - nothing bundled, nothing to license) and composited BEHIND the title
text so the words stay fully readable. Everything is deterministic (fixed seed),
so parallel render workers produce identical frames.

Public API: `make_decor_clip(kind, size, duration)` -> a MoviePy clip (with an
alpha mask) to layer over a title card, or None for an unknown/empty kind.
"""

import math

import numpy as np
from PIL import Image, ImageDraw
from moviepy import VideoClip

_SEED = 20261024

# Festive palettes (RGB).
_FESTIVE = [(224, 80, 70), (62, 122, 170), (221, 160, 60), (90, 160, 100), (200, 90, 140)]
_ROSE = [(206, 92, 108), (222, 132, 150), (188, 74, 94)]
_BRIGHT = [(255, 120, 100), (120, 200, 255), (255, 210, 90), (130, 230, 150), (230, 140, 210)]


def _fade_in(t, start, span=0.35):
    return max(0.0, min(1.0, (t - start) / span))


class _Balloons:
    """Balloons rising from below, with a gentle sway and a string."""
    def __init__(self, w, h, dur, n=7):
        rng = np.random.default_rng(_SEED)
        self.w, self.h, self.dur = w, h, dur
        self.items = [dict(
            x=rng.uniform(0.07, 0.93) * w, r=rng.uniform(0.035, 0.055) * h,
            color=_FESTIVE[i % len(_FESTIVE)], start=rng.uniform(0, 0.35) * dur,
            speed=rng.uniform(0.85, 1.25), sway=rng.uniform(10, 28) * (w / 1920),
            swayf=rng.uniform(0.3, 0.7), phase=rng.uniform(0, 6.28),
        ) for i in range(n)]

    def draw(self, d, t):
        h = self.h
        for b in self.items:
            if t < b["start"]:
                continue
            p = (t - b["start"]) / max(0.1, self.dur) * b["speed"]
            r = b["r"]
            y = h + r * 1.6 - p * (h + r * 3.4)
            if y < -r * 2 or y > h + r * 2:
                continue
            x = b["x"] + b["sway"] * math.sin(b["swayf"] * t * 6.28 + b["phase"])
            a = int(205 * _fade_in(t, b["start"]))
            c = b["color"]
            d.ellipse([x - r, y - r * 1.2, x + r, y + r * 1.2], fill=c + (a,))
            # highlight + knot + string
            d.ellipse([x - r * 0.5, y - r * 0.8, x - r * 0.1, y - r * 0.3],
                      fill=(255, 255, 255, int(a * 0.35)))
            knot = y + r * 1.2
            d.polygon([(x - 3, knot), (x + 3, knot), (x, knot + 6)], fill=c + (a,))
            d.line([(x, knot + 6), (x + b["sway"] * 0.3, knot + 6 + r * 1.7)],
                   fill=(120, 110, 100, int(a * 0.6)), width=2)


class _Hearts:
    """Hearts drifting up and fading - a soft, romantic motif."""
    def __init__(self, w, h, dur, n=10):
        rng = np.random.default_rng(_SEED)
        self.w, self.h, self.dur = w, h, dur
        self.items = [dict(
            x=rng.uniform(0.05, 0.95) * w, s=rng.uniform(0.018, 0.04) * h,
            color=_ROSE[i % len(_ROSE)], start=rng.uniform(0, 0.8) * dur,
            speed=rng.uniform(0.6, 1.0), sway=rng.uniform(8, 22) * (w / 1920),
            swayf=rng.uniform(0.3, 0.7), phase=rng.uniform(0, 6.28),
        ) for i in range(n)]

    def draw(self, d, t):
        h = self.h
        for it in self.items:
            if t < it["start"]:
                continue
            p = (t - it["start"]) / max(0.1, self.dur) * it["speed"]
            y = h + 40 - p * (h + 80)
            if y < -40:
                continue
            x = it["x"] + it["sway"] * math.sin(it["swayf"] * t * 6.28 + it["phase"])
            a = int(190 * _fade_in(t, it["start"]) * max(0.0, 1.0 - p))
            if a <= 0:
                continue
            self._heart(d, x, y, it["s"], it["color"] + (a,))

    @staticmethod
    def _heart(d, x, y, s, rgba):
        d.ellipse([x - s, y - s * 0.6, x, y + s * 0.2], fill=rgba)
        d.ellipse([x, y - s * 0.6, x + s, y + s * 0.2], fill=rgba)
        d.polygon([(x - s * 0.96, y - s * 0.02), (x + s * 0.96, y - s * 0.02),
                   (x, y + s * 1.1)], fill=rgba)


class _Confetti:
    """Falling, rotating confetti rectangles."""
    def __init__(self, w, h, dur, n=60):
        rng = np.random.default_rng(_SEED)
        self.w, self.h, self.dur = w, h, dur
        self.items = [dict(
            x=rng.uniform(0, 1) * w, y0=rng.uniform(-0.3, 1.0) * h,
            size=rng.uniform(6, 12) * (w / 1920), color=_BRIGHT[i % len(_BRIGHT)],
            speed=rng.uniform(0.6, 1.3), sway=rng.uniform(10, 40) * (w / 1920),
            swayf=rng.uniform(0.6, 1.4), spin=rng.uniform(1, 4), phase=rng.uniform(0, 6.28),
        ) for i in range(n)]

    def draw(self, d, t):
        w, h = self.w, self.h
        for it in self.items:
            y = (it["y0"] + t * it["speed"] * h * 0.45) % (h + 40) - 20
            x = (it["x"] + it["sway"] * math.sin(it["swayf"] * t * 6.28 + it["phase"])) % w
            s = it["size"]
            ang = it["spin"] * t * 6.28 + it["phase"]
            ca, sa = math.cos(ang), math.sin(ang)
            pts = [(-s, -s * 0.5), (s, -s * 0.5), (s, s * 0.5), (-s, s * 0.5)]
            poly = [(x + px * ca - py * sa, y + px * sa + py * ca) for px, py in pts]
            d.polygon(poly, fill=it["color"] + (190,))


class _Fireworks:
    """Staggered particle bursts that expand and fade."""
    def __init__(self, w, h, dur, n=5):
        rng = np.random.default_rng(_SEED)
        self.w, self.h, self.dur = w, h, dur
        self.bursts = []
        for i in range(n):
            k = rng.integers(16, 26)
            self.bursts.append(dict(
                cx=rng.uniform(0.15, 0.85) * w, cy=rng.uniform(0.15, 0.55) * h,
                start=rng.uniform(0.1, 0.85) * dur, life=rng.uniform(0.9, 1.4),
                maxr=rng.uniform(0.1, 0.18) * h, color=_BRIGHT[i % len(_BRIGHT)],
                ang=np.linspace(0, 6.28, int(k), endpoint=False),
            ))

    def draw(self, d, t):
        scale = self.w / 1920
        for b in self.bursts:
            dt = t - b["start"]
            if dt < 0 or dt > b["life"]:
                continue
            p = dt / b["life"]
            c = b["color"]
            # A bright core flash at ignition, fading fast.
            if p < 0.22:
                fa = int(235 * (1 - p / 0.22))
                cr = max(4, int(10 * scale))
                d.ellipse([b["cx"] - cr, b["cy"] - cr, b["cx"] + cr, b["cy"] + cr],
                          fill=(255, 248, 225, fa))
            r = b["maxr"] * (1 - (1 - p) ** 2)          # ease-out expand
            a = int(235 * max(0.0, 1 - p))               # fade over life
            drop = b["maxr"] * 0.35 * p * p               # slight gravity
            ps = max(2, int(5 * scale))
            for ang in b["ang"]:
                x = b["cx"] + r * math.cos(ang)
                y = b["cy"] + r * math.sin(ang) + drop
                d.ellipse([x - ps, y - ps, x + ps, y + ps], fill=c + (a,))
                # a short spark tail toward the centre
                xt = b["cx"] + r * 0.8 * math.cos(ang)
                yt = b["cy"] + r * 0.8 * math.sin(ang) + drop
                d.line([(xt, yt), (x, y)], fill=c + (int(a * 0.5),), width=max(1, ps // 2))


_MOTIFS = {"balloons": _Balloons, "hearts": _Hearts, "confetti": _Confetti,
           "fireworks": _Fireworks}


def make_decor_clip(kind: str, size: tuple, duration: float):
    """A MoviePy clip (RGB + alpha mask) drawing `kind` over `duration`, or None.
    Sized to `size` (the title canvas). Layer it ABOVE the background and BELOW
    the title text."""
    cls = _MOTIFS.get(kind or "")
    if cls is None:
        return None
    w, h = int(size[0]), int(size[1])
    motif = cls(w, h, float(duration))
    cache: dict[float, Image.Image] = {}

    def _rgba(t):
        k = round(float(t), 3)
        img = cache.get(k)
        if img is None:
            img = Image.new("RGBA", (w, h), (0, 0, 0, 0))
            motif.draw(ImageDraw.Draw(img, "RGBA"), float(t))
            if len(cache) > 6:
                cache.clear()
            cache[k] = img
        return img

    def frame(t):
        return np.asarray(_rgba(t).convert("RGB"))

    def mask(t):
        return np.asarray(_rgba(t))[:, :, 3].astype("float32") / 255.0

    clip = VideoClip(frame_function=frame, duration=float(duration))
    mask_clip = VideoClip(frame_function=mask, is_mask=True, duration=float(duration))
    return clip.with_mask(mask_clip)
