"""MoviePy/ffmpeg generate step (Next Steps step 8, docs/pacing_design.md).

Targets MoviePy 2.x (no `moviepy.editor` module - imports come straight from
`moviepy`; `set_*` methods became `with_*`; `subclip` became `subclipped`;
fades are applied as effects via `vfx`/`afx` rather than dedicated methods).

Caption layering:
- Trip intro: a standalone opening slide with free-text intro (trip_intro).
- Day intro: EACH day gets its own standalone slide with that day's
  description (day_captions) - not an overlay on a photo.
- Place tag: the auto-resolved cluster place_name, shown ONLY on the first
  item of each cluster (moment) - a quick label at the bottom.

Audio layering: background music plays as a continuous bed under the whole
film. Under title slides and photos it sits at full background level; under a
video clip it DUCKS to a soft bed (it never fully leaves) while the clip's own
original sound - faded in and out, not hard-cut - plays on top. The music eases
down into the duck and back up out of it rather than restarting or overlapping.

Transitions: photo<->clip boundaries dip through black + near-silence on BOTH
sides. Photos dip to/from black; video clips get the same short dip (they used
to hard-cut), and the clip's native audio fades with the picture, so the two
media meet at a shared neutral point (DESIGN.md: "cross-dissolves between
segments", gentle motion).
"""

import multiprocessing as mp
import os
import shutil
import subprocess
import sys
import tempfile
import time
from datetime import datetime
from pathlib import Path

from imageio_ffmpeg import get_ffmpeg_exe

import numpy as np
from PIL import Image
from bidi.algorithm import get_display

from moviepy import (
    AudioFileClip,
    ColorClip,
    CompositeVideoClip,
    ImageClip,
    TextClip,
    VideoFileClip,
    afx,
    vfx,
    concatenate_audioclips,
    concatenate_videoclips,
)
from moviepy.audio.AudioClip import AudioClip, CompositeAudioClip
from proglog import ProgressBarLogger

from generate.pacing import item_duration, DEFAULT_PHOTO_DURATION
from ingest.image_utils import load_corrected_rgb, apply_crop
from config import get_setting, get_bool, ordered_music_files

OUTPUT_DIR = Path("data") / "output"
MUSIC_DIR = Path("data") / "music"  # playlist folder, user-supplied, gitignored -
                                      # tracks play back-to-back in filename order;
                                      # prefix with numbers (01-, 02-...) to control
                                      # order if it matters
FADE_SECONDS = 2.0
MUSIC_CLIP_FADE = 1.0  # music fades out over this many seconds before a video
                       # clip starts, and fades back in after it ends, instead
                       # of hard-cutting (video-review feedback)
MUSIC_VOLUME = 0.4     # background music level under photos/titles. Full-volume
                       # music drowned out the (typically quieter) native audio
                       # of video clips, making clips feel much softer than the
                       # music - lower the music so it sits *under* everything.
MUSIC_DUCK_LEVEL = 0.12  # music does NOT stop under a video clip - it ducks to
                       # this soft bed and keeps playing, so the soundtrack never
                       # fully drops out (which felt like the audio "left the
                       # room"). The clip's native sound sits on top of this bed;
                       # the ramp between MUSIC_VOLUME and MUSIC_DUCK_LEVEL is what
                       # makes entering/leaving a clip feel gentle.
CLIP_AUDIO_GAIN = 1.5  # modest boost to a clip's own recorded sound so it comes
                       # through clearly over the ducked music bed. Kept
                       # conservative to avoid distorting/clipping loud clips.
MUSIC_TRACK_CROSSFADE = 0.8  # at a per-moment soundtrack handoff, the outgoing
                       # track fades out and the incoming one fades in over this
                       # many seconds - a clean breath between two different songs
                       # rather than a hard cut or a muddy overlap of two keys
CLIP_AUDIO_FADE = 0.5  # a clip's native audio fades in/out over this many seconds
                       # instead of punching in at full and cutting out dead -
                       # matched to the picture dip (FADE_DURATION) so sound and
                       # image ease through the transition together.
KEN_BURNS_ZOOM_RATE = 0.02  # 2% zoom per second
# Photo motion style (config `motion_style`): "fade" (default) dip-to-black in/out,
# cheap; "static" holds the still, cheapest; "ken_burns" the slow per-frame zoom.
FADE_DURATION = 0.5  # seconds of fade in AND out for the "fade" motion style
FPS = 24  # output frame rate; also the grid every segment duration is snapped to
          # (see _snap) so parallel-rendered chunks concatenate with zero A/V drift.
BASE_SIZE = (1920, 1080)  # full-quality canvas: every frame is composited onto a
                            # canvas of exactly this size so the output is a uniform
                            # 16:9 video. Without this, mixing a portrait clip (e.g.
                            # 1088x1920) with landscape content made concatenate(compose)
                            # pick a 1920x1920 square canvas - the video then filled
                            # only a centered square of a normal screen (review feedback).
DRAFT_SCALE = 1 / 3  # draft canvas = 640x360: ~9x fewer pixels than 1080p. The encode
                     # is producer-bound (single-threaded frame generation feeding
                     # ffmpeg), so fewer pixels per frame is the dominant speedup -
                     # fast to render but still clearly viewable for checking the cut.
# TARGET_SIZE / MAX_RENDER_SIZE (and the caption metrics below) are the ACTIVE render
# geometry, set per run by _apply_render_scale(); they default to full quality here.
TARGET_SIZE = BASE_SIZE
MAX_RENDER_SIZE = TARGET_SIZE  # downscale target before per-frame zoom - see note below
from paths import resource_path
FONT_DIR = resource_path("generate/fonts")
# Bilingual (Hebrew + Latin) keepsake fonts, bundled in the repo (DESIGN.md).
# Frank Ruhl Libre for title cards, Assistant for place captions - both are
# Hebrew-native, replacing the old C:/Windows/Fonts/arial.ttf.
TITLE_FONT = str(FONT_DIR / "FrankRuhlLibre-Bold.ttf")
CAPTION_FONT = str(FONT_DIR / "Assistant-SemiBold.ttf")

# Warm keepsake palette for the video (DESIGN.md, Direction A).
TITLE_CARD_BG = (247, 241, 230)   # cream #F7F1E6 - default title-card background
TITLE_CARD_TEXT = "#2B2724"       # ink - title text on the cream card
TITLE_CARD_BG_ALT = (36, 31, 27)  # espresso #241F1B alternate (pair with CREAM text)
CREAM = "#F7F1E6"                 # caption text color
SCRIM_ESPRESSO = (43, 39, 36)     # #2B2724 - warm band behind place captions
SCRIM_OPACITY = 0.55

# Selectable video look (Setup -> Settings). Each style sets the title-card
# background/text/font and the place-caption text + band colours.
VIDEO_STYLES = {
    "keepsake": {  # warm cream card, ink serif (default)
        "title_bg": (247, 241, 230), "title_text": "#2B2724", "title_font": TITLE_FONT,
        "caption_color": CREAM, "scrim_color": SCRIM_ESPRESSO,
    },
    "espresso": {  # dark, cinematic
        "title_bg": (36, 31, 27), "title_text": CREAM, "title_font": TITLE_FONT,
        "caption_color": CREAM, "scrim_color": SCRIM_ESPRESSO,
    },
    "minimal": {  # clean, near-white, humanist sans
        "title_bg": (250, 250, 248), "title_text": "#2B2724", "title_font": CAPTION_FONT,
        "caption_color": CREAM, "scrim_color": SCRIM_ESPRESSO,
    },
}


def _active_style() -> dict:
    return VIDEO_STYLES.get(get_setting("video_style", "keepsake"), VIDEO_STYLES["keepsake"])

CAPTION_MAX_WIDTH = MAX_RENDER_SIZE[0] - 240  # leaves side margin, forces wrapping
TITLE_BOX_HEIGHT = 500  # generous explicit height - the previous auto-height
                         # ("size=(width, None)") was under-computing for
                         # wrapped multi-line text, clipping the lower lines
PLACE_TAG_BOX_HEIGHT = 160
TITLE_FONT_SIZE = 44   # title-card text; scaled with the canvas in draft mode
PLACE_FONT_SIZE = 28   # place-tag caption text; scaled with the canvas in draft mode


def _apply_render_scale(scale: float) -> None:
    """Set the ACTIVE render geometry to `scale` x the full-quality 1080p layout.
    Draft mode shrinks the whole 16:9 layout - canvas, caption widths, box heights
    and font sizes together - so a preview stays proportional, just smaller and much
    faster to composite. scale=1.0 is full quality. Mutates module globals that the
    build functions read at call time; generate_video resets it to 1.0 when done, and
    each parallel worker sets its own scale on startup."""
    global TARGET_SIZE, MAX_RENDER_SIZE, CAPTION_MAX_WIDTH
    global TITLE_BOX_HEIGHT, PLACE_TAG_BOX_HEIGHT, TITLE_FONT_SIZE, PLACE_FONT_SIZE
    w = round(BASE_SIZE[0] * scale)
    h = round(BASE_SIZE[1] * scale)
    w -= w % 2  # even dimensions required by yuv420p / libx264
    h -= h % 2
    TARGET_SIZE = (w, h)
    MAX_RENDER_SIZE = TARGET_SIZE
    CAPTION_MAX_WIDTH = TARGET_SIZE[0] - round(240 * scale)
    TITLE_BOX_HEIGHT = round(500 * scale)
    PLACE_TAG_BOX_HEIGHT = round(160 * scale)
    # Floor the fonts so a small draft canvas keeps captions legible (a strictly
    # proportional 28*scale would be unreadable at 360p); titles stay proportional.
    TITLE_FONT_SIZE = max(16, round(44 * scale))
    PLACE_FONT_SIZE = max(13, round(28 * scale))


def _snap(duration: float) -> float:
    """Round a duration to a whole number of frames. Every segment is frame-exact
    so that independently-encoded parallel chunks concatenate with no accumulating
    A/V drift, and each segment's audio offset lines up with its picture."""
    return max(1, round(duration * FPS)) / FPS


INTRO_DURATION = 5.0
DAY_INTRO_DURATION = 4.0
OUTRO_DURATION = 5.0


class _EncodeProgressLogger(ProgressBarLogger):
    """Bridges MoviePy/ffmpeg's write progress to our progress_cb so the UI can
    show an encode bar during the long final render instead of looking frozen."""

    def __init__(self, progress_cb):
        super().__init__()
        self._cb = progress_cb

    def bars_callback(self, bar, attr, value, old_value=None):
        if attr != "index":
            return
        total = self.bars.get(bar, {}).get("total")
        if not total:
            return
        # write_videofile runs TWO passes with distinct proglog bars: the audio
        # mux ("chunk") first, then the video frames ("t"). They used to share one
        # "Encoding video…" message, so the bar appeared to fill up, reset, and
        # fill again under a single step. Give each pass its own message so the UI
        # can show them as two separate phases (soundtrack, then picture).
        message = "Encoding the soundtrack…" if bar == "chunk" else "Encoding video…"
        try:
            self._cb(value, total, message)
        except Exception:
            pass


def _rtl(text: str) -> str:
    """Reorders bidirectional text (Hebrew/Arabic) into visual order -
    Pillow's text rendering doesn't do this itself."""
    return get_display(text)


def _format_day_header(iso_date: str, description: str) -> str:
    date_display = datetime.strptime(iso_date, "%Y-%m-%d").strftime("%d/%m/%Y")
    return f"{date_display}\n{description}" if description else date_display


def _ken_burns(clip):
    return clip.resized(lambda t: 1 + KEN_BURNS_ZOOM_RATE * t)


def _fit_to_canvas(clip, duration: float):
    """Scale `clip` to fit inside TARGET_SIZE (preserving aspect ratio) and
    center it on a solid black canvas of exactly TARGET_SIZE, so every frame
    of the final video has identical dimensions. Portrait content is
    pillarboxed rather than blowing the canvas out to a square."""
    tw, th = TARGET_SIZE
    cw, ch = clip.size
    scale = min(tw / cw, th / ch)
    fitted = clip.resized(scale) if scale != 1 else clip
    background = ColorClip(size=TARGET_SIZE, color=(0, 0, 0)).with_duration(duration)
    return CompositeVideoClip(
        [background, fitted.with_position("center")], size=TARGET_SIZE
    ).with_duration(duration)


def _text_overlay(clip, text: str, position: tuple, font_size: int, box_height: int,
                  font: str = CAPTION_FONT, color: str = "white",
                  stroke_color: str = "black", stroke_width: int = 2,
                  scrim: bool = False, scrim_color: tuple = SCRIM_ESPRESSO,
                  scrim_opacity: float = SCRIM_OPACITY):
    """An explicit `size` (both width AND height, not height=None) with
    `vertical_align="center"` is the fix for text being clipped from below -
    leaving height unset let MoviePy under-compute the box for wrapped
    multi-line text.

    `scrim=True` lays a semi-transparent warm band behind the text so cream
    captions stay legible over bright photo areas (sky, sand, snow) - a thin
    stroke alone washed out against light backgrounds (review feedback).
    Color/font are parametrized so title cards (ink serif on cream) and place
    captions (cream sans on an espresso band) share one renderer."""
    caption = TextClip(
        text=_rtl(text), font=font, font_size=font_size, color=color,
        stroke_color=stroke_color, stroke_width=stroke_width,
        method="caption", size=(CAPTION_MAX_WIDTH, box_height),
        text_align="center", vertical_align="center",
    ).with_duration(clip.duration).with_position(position, relative=True)
    layers = [clip]
    if scrim:
        band = (
            ColorClip(size=(CAPTION_MAX_WIDTH, box_height), color=scrim_color)
            .with_opacity(scrim_opacity)
            .with_duration(clip.duration)
            .with_position(position, relative=True)
        )
        layers.append(band)
    layers.append(caption)
    return CompositeVideoClip(layers)


def _title_slide(text: str, duration: float):
    if not text:
        return None
    # Title card colours + font come from the selected video style (Settings).
    style = _active_style()
    background = ColorClip(size=MAX_RENDER_SIZE, color=style["title_bg"]).with_duration(duration)
    return _text_overlay(
        background, text, ("center", "center"), TITLE_FONT_SIZE, TITLE_BOX_HEIGHT,
        font=style["title_font"], color=style["title_text"],
        stroke_color=style["title_text"], stroke_width=0,
    )


def _build_item_clip(row, show_place_tag: bool, motion: str = "fade",
                     photo_seconds: float = DEFAULT_PHOTO_DURATION):
    path = row["path"]
    if row["item_type"] == "photo":
        duration = _snap(item_duration("photo", row["duration_override"], photo_default=photo_seconds))
        rgb = load_corrected_rgb(path)
        rotation = (row["rotation"] or 0) % 360  # user rotate (lightbox), clockwise
        if rotation:
            rgb = np.rot90(rgb, -(rotation // 90) % 4)
        rgb = apply_crop(rgb, row["crop"])  # user crop (lightbox), relative to rotated frame
        img = Image.fromarray(rgb)
        # Downscale to the render size - "ken_burns" resizes on EVERY output frame
        # (what made a 19-photo batch take ~30 min), so downscale here first; the
        # cheaper "fade"/"static" styles skip the per-frame resize entirely.
        img.thumbnail(MAX_RENDER_SIZE, Image.LANCZOS)
        clip = ImageClip(np.array(img)).with_duration(duration)
        if motion == "ken_burns":
            clip = _ken_burns(clip)
        has_own_audio = False
    else:
        raw = VideoFileClip(path)
        # User trim (lightbox) via clip_start/clip_end; falls back to the full
        # clip. Bounds-checked so a bad in/out can't crash the render.
        start = max(0.0, float(row["clip_start"] or 0.0))
        end = float(row["clip_end"]) if row["clip_end"] else raw.duration
        start = min(start, raw.duration)
        end = min(end, raw.duration)
        if end <= start:
            end = raw.duration
        clip = raw.subclipped(start, end)
        duration = _snap(end - start)
        has_own_audio = clip.audio is not None

    # Native clip audio must be captured BEFORE _fit_to_canvas composites the
    # visual onto a silent black background (the composite's .audio would be
    # None otherwise).
    native_audio = clip.audio if has_own_audio else None

    # Normalize every item to the same 16:9 canvas so the output isn't a
    # square/letterboxed frame when portrait and landscape media are mixed.
    clip = _fit_to_canvas(clip, duration)

    # Place caption: a photo's OWN place (review "Find place", per-photo) shows on
    # that photo; otherwise the moment/cluster place shows on the first photo of
    # the group. Positioned near the bottom on a dark scrim for legibility.
    caption = row["item_place"] or (row["cluster_place"] if show_place_tag else None)
    if caption:
        style = _active_style()
        clip = _text_overlay(
            clip, caption, ("center", 0.83), PLACE_FONT_SIZE, PLACE_TAG_BOX_HEIGHT,
            color=style["caption_color"], stroke_color="#2B2724", stroke_width=1,
            scrim=True, scrim_color=style["scrim_color"],
        )

    # A static/fade photo is IDENTICAL on every frame, but the composite above
    # (black canvas + fitted photo + caption) is re-rendered ~24x/sec at encode
    # time - the dominant cost of the slow "Encoding video" pass. Render it ONCE
    # here and repeat that single frame as a plain ImageClip, so the encode just
    # pipes the same array (x264-bound, hundreds of fps) instead of recompositing
    # in Python. Ken Burns genuinely changes every frame, so it's left live.
    if row["item_type"] == "photo" and motion in ("static", "fade"):
        clip = ImageClip(np.asarray(clip.get_frame(0), dtype="uint8")).with_duration(duration)

    # "fade" motion: dip the whole slide (photo + caption) in from / out to black,
    # WITHIN its own duration so item timing - and the separately-built audio
    # track - stay in sync. Applied to photos only; video clips carry their own
    # motion. Cheap: an alpha ramp during the fade windows, no per-frame resize.
    if row["item_type"] == "photo" and motion == "fade":
        fade = min(FADE_DURATION, duration / 2)
        clip = clip.with_effects([vfx.FadeIn(fade), vfx.FadeOut(fade)])

    # Video clips used to hard-cut in and out while the adjacent photos dipped to
    # black - one gentle side, one jarring side. Give the clip the SAME short dip
    # so a photo<->clip boundary meets black-on-black (a cross-dissolve through
    # black). Only in "fade" mode: in "static"/"ken_burns" the photos hard-cut, so
    # clips hard-cut too and the two sides still match. The clip's native audio is
    # faded to match in _build_audio_track.
    if row["item_type"] == "clip" and motion == "fade":
        fade = min(FADE_DURATION, duration / 2)
        clip = clip.with_effects([vfx.FadeIn(fade), vfx.FadeOut(fade)])
    return clip, duration, has_own_audio, native_audio


def _silence(duration: float):
    return AudioClip(lambda t: 0, duration=duration, fps=44100)


def _track_source_for(path, length: float):
    """A region's soundtrack as an AudioClip covering exactly `length` seconds.

    `path` is either ONE file (a per-moment cue: that song, from its start) or a
    LIST of files (the no-cue playlist default: every track played back-to-back
    in order). Either way the source is looped (concatenated with itself) if it's
    shorter than the region it has to fill, then trimmed to `length`. A region's
    track always begins at its own beginning, so a song starts where the user
    dropped it / the playlist starts on track 1."""
    paths = path if isinstance(path, (list, tuple)) else [path]
    src = concatenate_audioclips([AudioFileClip(str(p)) for p in paths])
    if src.duration < length:
        loops = int(length // src.duration) + 1
        src = concatenate_audioclips([src] * loops)
    return src.subclipped(0, length)


def _native(native):
    """A video clip's own recorded sound: boosted so it reads over the ducked
    music bed, and faded in/out (not hard-cut) so it eases in as the music ducks
    down and eases out as the music swells back - matched to the picture dip."""
    fade = min(CLIP_AUDIO_FADE, native.duration / 2)
    return native.with_effects([
        afx.MultiplyVolume(CLIP_AUDIO_GAIN),
        afx.AudioFadeIn(fade),
        afx.AudioFadeOut(fade),
    ])


def _build_audio_track(segments: list[tuple[float, bool, object, object]], total_duration: float):
    """segments: list of (duration, has_own_audio, native_audio_or_None,
    track_path_or_None) in timeline order. `track_path` is the soundtrack active
    during that segment; it stays constant across a run of moments and changes
    only where the user started a new track (the per-moment cue), so equal
    consecutive paths form one contiguous music REGION.

    Within each region the layered ducking is exactly as before:
    - a soft music bed at MUSIC_DUCK_LEVEL plays under everything in the region;
    - an "extra" layer, sliced from the SAME region source at the same offsets,
      adds the rest of the volume (up to MUSIC_VOLUME) under title/photo segments
      and fades to the bed around clips;
    - each clip's own audio (boosted + faded, see _native) sits on top.

    At a region boundary the two soundtracks butt-join with a short fade out/in
    (MUSIC_TRACK_CROSSFADE) on both the bed and the extra layer - a clean handoff
    between songs. Each region's track plays from its own start."""
    def _is_clip(seg) -> bool:
        _duration, has_own_audio, native, _track = seg
        return has_own_audio and native is not None

    has_music = any(seg[3] is not None for seg in segments)

    # No music at all: just the clips' own (faded) audio dropped onto a silent bed.
    if not has_music:
        layers = [_silence(total_duration)]
        off = 0.0
        for duration, has_own_audio, native, _track in segments:
            if has_own_audio and native is not None:
                layers.append(_native(native).with_start(off))
            off += duration
        return CompositeAudioClip(layers).with_duration(total_duration)

    # Lay out the timeline: per-segment start offset, and group runs of the same
    # track into regions (start, length, path).
    n = len(segments)
    seg_off = [0.0] * n
    region_id = [0] * n
    regions: list[list] = []  # [start, length, path]
    off = 0.0
    for i, (duration, _has, _nat, track) in enumerate(segments):
        if not regions or regions[-1][2] != track:
            regions.append([off, 0.0, track])
        seg_off[i] = off
        region_id[i] = len(regions) - 1
        regions[-1][1] += duration
        off += duration

    # One looped/trimmed source per region (each track from its own start).
    region_source = [_track_source_for(path, length) for (_start, length, path) in regions]
    last_region = len(regions) - 1
    extra_gain = MUSIC_VOLUME - MUSIC_DUCK_LEVEL

    layers = []
    # Soft continuous bed per region, with a fade at each track handoff.
    for rid, (start, length, _path) in enumerate(regions):
        xf = min(MUSIC_TRACK_CROSSFADE, length / 2)
        bed_fx = [afx.MultiplyVolume(MUSIC_DUCK_LEVEL)]
        if rid > 0:
            bed_fx.append(afx.AudioFadeIn(xf))
        if rid < last_region:
            bed_fx.append(afx.AudioFadeOut(xf))
        layers.append(region_source[rid].with_effects(bed_fx).with_start(start))

    # Extra music (photos/titles) + native clip audio at their exact offsets.
    for i, (duration, has_own_audio, native, _track) in enumerate(segments):
        if has_own_audio and native is not None:
            layers.append(_native(native).with_start(seg_off[i]))
            continue
        rid = region_id[i]
        rstart, rlength, _path = regions[rid]
        local = seg_off[i] - rstart
        piece = region_source[rid].subclipped(local, local + duration).with_effects(
            [afx.MultiplyVolume(extra_gain)]
        )
        effects = []
        # Ramp the extra layer down to the bed before a clip and back up after.
        clip_fade = min(MUSIC_CLIP_FADE, duration / 2)
        if i + 1 < n and _is_clip(segments[i + 1]):
            effects.append(afx.AudioFadeOut(clip_fade))
        if i > 0 and _is_clip(segments[i - 1]):
            effects.append(afx.AudioFadeIn(clip_fade))
        # Fade the extra at a track handoff too (region edge), matching the bed.
        xf = min(MUSIC_TRACK_CROSSFADE, rlength / 2)
        if rid > 0 and (i == 0 or region_id[i - 1] != rid):
            effects.append(afx.AudioFadeIn(xf))
        if rid < last_region and (i == n - 1 or region_id[i + 1] != rid):
            effects.append(afx.AudioFadeOut(xf))
        if effects:
            piece = piece.with_effects(effects)
        layers.append(piece.with_start(seg_off[i]))

    track = CompositeAudioClip(layers).with_duration(total_duration)
    # Overall fade at the very start/end of the whole soundtrack.
    return track.with_effects([afx.AudioFadeIn(FADE_SECONDS), afx.AudioFadeOut(FADE_SECONDS)])


_ITEMS_SQL = """SELECT i.path, i.item_type, i.duration_override, i.cluster_id,
                  i.rotation, i.clip_start, i.clip_end, i.crop,
                  i.place_name AS item_place,
                  c.trip_day, c.place_name AS cluster_place, c.music_track,
                  dc.caption_text AS day_caption
           FROM items i
           JOIN clusters c ON c.id = i.cluster_id
           LEFT JOIN day_captions dc ON dc.trip_day = c.trip_day
           WHERE i.include_in_video = 1
           ORDER BY c.trip_day,
                    (SELECT MIN(i2.timestamp) FROM items i2
                     WHERE i2.cluster_id = c.id AND i2.include_in_video = 1),
                    i.cluster_id, i.sort_order, i.timestamp"""


def _build_spec(spec, motion, photo_seconds):
    """Build one visual clip from a picklable spec (see _plan). Runs in whichever
    process renders the segment - the main process for a single pass, or a worker for
    just its slice - so a given process decodes ONLY the photos it actually renders.
    That bounded per-process footprint is the point of the plan/spec split."""
    if spec["kind"] == "title":
        return _title_slide(spec["text"], spec["duration"])
    clip, _dur, _has, _native = _build_item_clip(
        spec["row"], spec["show_place_tag"], motion, photo_seconds)
    return clip


def _plan(conn, progress_cb=None):
    """Cheap planning pass: the ordered list of clip SPECS (picklable build
    instructions) and the parallel audio-segment list - WITHOUT decoding any photo.
    A spec is either a title card (text + duration) or an item (its catalog row +
    whether it carries a place tag). Only video items are opened here, to capture
    native audio + exact duration for the soundtrack; a photo's duration comes from
    settings alone. Workers later build only the specs in their slice, so no process
    ever holds the whole flattened timeline.

    Returns (specs, audio_segments, motion, photo_seconds, n_photos, n_clips).

    INNER JOIN on clusters: an item with no cluster has no trip_day/place and can't sit
    under a day card; ordering groups clusters by day then earliest timestamp so the
    story stays chronological regardless of manual sort_order."""
    rows = conn.execute(_ITEMS_SQL).fetchall()
    if not rows:
        raise ValueError("no items included - nothing to generate")

    orphan_count = conn.execute(
        "SELECT COUNT(*) AS n FROM items WHERE include_in_video = 1 AND cluster_id IS NULL"
    ).fetchone()["n"]
    if orphan_count:
        print(
            f"note: skipping {orphan_count} included item(s) with no cluster "
            "(no day/time) - assign them to a cluster in the review UI to include them."
        )

    specs = []           # {"kind":"title",text,duration} | {"kind":"item",row,show_place_tag}
    audio_segments = []   # (duration, has_own_audio, native_audio, track_path)

    # Soundtrack: the ordered tracks, and a lookup by filename for the per-moment
    # cue (clusters.music_track).
    #
    # Two modes:
    #  * CUED - the user placed at least one "Soundtrack from here" cue. current_track
    #    starts on the first track and switches at each cued moment; equal runs form
    #    music regions (the fine-grained, hand-arranged soundtrack).
    #  * PLAYLIST (default when there are 2+ tracks and NO cues) - play every track
    #    back-to-back in order, looping the set to fill the film. This is what a user
    #    who just dropped several songs into the music folder expects: all of them
    #    get used, not only the first looped forever. A single shared list object is
    #    the "track" for every segment, so the whole film is one region whose source
    #    is the concatenated playlist. Placing any cue switches back to CUED mode.
    track_paths = ordered_music_files(get_setting("music_dir", str(MUSIC_DIR)))
    track_by_name = {p.name: p for p in track_paths}
    has_cues = any((r["music_track"] or "").strip() in track_by_name for r in rows)
    playlist_mode = len(track_paths) >= 2 and not has_cues
    if playlist_mode:
        current_track = track_paths           # one region, playlist as its source
    else:
        current_track = track_paths[0] if track_paths else None

    intro_row = conn.execute(
        "SELECT intro_text, outro_text FROM trip_intro WHERE id = 1"
    ).fetchone()
    intro_text = (intro_row["intro_text"] if intro_row else "") or ""
    if intro_text:
        specs.append({"kind": "title", "text": intro_text, "duration": INTRO_DURATION})
        audio_segments.append((_snap(INTRO_DURATION), False, None, current_track))

    motion = get_setting("motion_style", "fade")  # fade | static | ken_burns
    try:
        photo_seconds = float(get_setting("photo_duration_seconds", DEFAULT_PHOTO_DURATION))
    except (TypeError, ValueError):
        photo_seconds = DEFAULT_PHOTO_DURATION

    n_photos = n_clips = 0
    seen_days = set()
    seen_clusters = set()
    for idx, row in enumerate(rows, start=1):
        if progress_cb is not None:
            progress_cb(idx, len(rows), f"Preparing your video… ({idx}/{len(rows)})")

        is_first_of_cluster = row["cluster_id"] is not None and row["cluster_id"] not in seen_clusters
        # Resolve the per-moment soundtrack cue at the start of each moment, BEFORE
        # its day title card is emitted, so a track chosen on the first moment of a
        # day starts on that day's card rather than a beat late.
        if is_first_of_cluster and not playlist_mode and track_paths:
            cue = (row["music_track"] or "").strip()
            if cue and cue in track_by_name:
                current_track = track_by_name[cue]

        if row["trip_day"] is not None and row["trip_day"] not in seen_days:
            seen_days.add(row["trip_day"])
            day_header = _format_day_header(row["trip_day"], row["day_caption"] or "")
            if day_header:
                specs.append({"kind": "title", "text": day_header, "duration": DAY_INTRO_DURATION})
                audio_segments.append((_snap(DAY_INTRO_DURATION), False, None, current_track))

        if row["cluster_id"] is not None:
            seen_clusters.add(row["cluster_id"])

        if row["item_type"] == "photo":
            # Photo duration comes from settings - no image decode needed here.
            duration = _snap(item_duration("photo", row["duration_override"], photo_default=photo_seconds))
            has_own_audio, native_audio = False, None
            n_photos += 1
        else:
            # Open the video ONCE here for its exact duration + native audio (the
            # soundtrack needs both, and it stays referenced via audio_segments); the
            # visual is rebuilt in whichever process renders this segment.
            _vis, duration, has_own_audio, native_audio = _build_item_clip(
                row, is_first_of_cluster, motion, photo_seconds)
            n_clips += 1

        specs.append({"kind": "item", "row": {k: row[k] for k in row.keys()},
                      "show_place_tag": is_first_of_cluster})
        audio_segments.append((duration, has_own_audio, native_audio, current_track))

    outro_text = (intro_row["outro_text"] if intro_row else "") or ""
    if outro_text:
        specs.append({"kind": "title", "text": outro_text, "duration": OUTRO_DURATION})
        audio_segments.append((_snap(OUTRO_DURATION), False, None, current_track))

    return specs, audio_segments, motion, photo_seconds, n_photos, n_clips


def _cut_times(durations):
    """Cumulative segment-boundary times - where H.264 keyframes are forced so no
    frame is predicted across a hard cut (would smear the previous slide otherwise)."""
    out, acc = [], 0.0
    for d in durations[:-1]:
        acc += d
        out.append(acc)
    return out


def _keyframe_params(cut_times):
    return (["-force_key_frames", ",".join(f"{t:.3f}" for t in cut_times)]
            if cut_times else None)


def _render_chunk(task):
    """Worker (own process): build ONLY the specs in its slice - so it decodes only
    that fraction of the photos - then encode them to a headerless, audio-less segment
    file. The build is deterministic, so segments have byte-identical stream params and
    copy-concatenate cleanly. Specs are plain dicts (picklable); no DB access needed."""
    k, specs, motion, photo_seconds, scale, out_path, preset = task
    _apply_render_scale(scale)
    clips = [_build_spec(s, motion, photo_seconds) for s in specs]
    video = concatenate_videoclips(clips, method="chain")
    # Pin pix_fmt so every segment has byte-identical stream params - a prerequisite
    # for stitching them later with ffmpeg's copy-concat (no re-encode).
    ffmpeg_params = ["-pix_fmt", "yuv420p"]
    ffmpeg_params += _keyframe_params(_cut_times([c.duration for c in clips])) or []
    video.write_videofile(
        str(out_path), fps=FPS, codec="libx264", audio=False,
        preset=preset, ffmpeg_params=ffmpeg_params, logger=None,
    )
    return k, str(out_path)


def _chunk_boundaries(durations, n):
    """Split the clip list into `n` contiguous [start, end) index ranges of roughly
    equal total duration, so parallel workers finish at about the same time. Never
    emits an empty range."""
    total = sum(durations)
    if n <= 1 or total <= 0 or len(durations) <= 1:
        return [(0, len(durations))]
    target = total / n
    bounds, start, acc = [], 0, 0.0
    for i, d in enumerate(durations):
        acc += d
        remaining_slots = n - len(bounds)
        remaining_items = len(durations) - (i + 1)
        if acc >= target and remaining_slots > 1 and remaining_items >= remaining_slots - 1:
            bounds.append((start, i + 1))
            start, acc = i + 1, 0.0
    bounds.append((start, len(durations)))
    return bounds


def _concat_and_mux(seg_files, audio_path, output_path):
    """Stitch the encoded segments (stream copy, no re-encode) via ffmpeg's concat
    demuxer and lay the single global soundtrack over them. Every segment shares
    codec/params and starts on a keyframe, so a copy-concat is clean and near-instant."""
    ffmpeg = get_ffmpeg_exe()
    tmpdir = Path(seg_files[0]).parent
    listfile = tmpdir / "segments.txt"
    listfile.write_text(
        "".join(f"file '{Path(s).as_posix()}'\n" for s in seg_files), encoding="utf-8"
    )
    cmd = [ffmpeg, "-y", "-f", "concat", "-safe", "0", "-i", str(listfile)]
    if audio_path is not None:
        cmd += ["-i", str(audio_path)]
    cmd += ["-map", "0:v:0"]
    if audio_path is not None:
        cmd += ["-map", "1:a:0", "-c:a", "copy", "-shortest"]
    cmd += ["-c:v", "copy", str(output_path)]
    subprocess.run(cmd, check=True, capture_output=True)


MAX_WORKERS = 8            # hard ceiling regardless of core count
WORKER_RAM_BUDGET = 0.60   # never plan workers to need more than this share of FREE RAM
_WORKER_BASE_BYTES = 400 * 1024**2  # per-process Python + moviepy/ffmpeg overhead


def _available_ram_bytes():
    """Best-effort free physical RAM (stdlib only; no psutil dependency).
    None if it can't be determined - callers then skip the memory cap."""
    try:
        if sys.platform == "win32":
            import ctypes

            class _MEMSTAT(ctypes.Structure):
                _fields_ = [("dwLength", ctypes.c_ulong), ("dwMemoryLoad", ctypes.c_ulong),
                            ("ullTotalPhys", ctypes.c_ulonglong), ("ullAvailPhys", ctypes.c_ulonglong),
                            ("ullTotalPageFile", ctypes.c_ulonglong), ("ullAvailPageFile", ctypes.c_ulonglong),
                            ("ullTotalVirtual", ctypes.c_ulonglong), ("ullAvailVirtual", ctypes.c_ulonglong),
                            ("ullAvailExtendedVirtual", ctypes.c_ulonglong)]

            stat = _MEMSTAT()
            stat.dwLength = ctypes.sizeof(stat)
            if ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(stat)):
                return int(stat.ullAvailPhys)
            return None
        return os.sysconf("SC_AVPHYS_PAGES") * os.sysconf("SC_PAGE_SIZE")
    except Exception:
        return None


def _plan_workers(n_photos: int) -> int:
    """How many render processes to run in parallel, bounded by BOTH:
      - CPU: leave a core free for the server/UI so the box stays responsive; and
      - RAM: with per-slice rendering each worker decodes only its OWN slice, so all
        workers together hold ~one flattened timeline (loaded once, spread across them),
        plus a fixed per-process overhead. Cap workers so timeline + workers*overhead
        stays under WORKER_RAM_BUDGET of free RAM. (Before per-slice, each worker held
        the WHOLE timeline, i.e. workers x timeline - far more memory-hungry.)
    Returns 1 when only a single worker fits - the caller then renders in-process."""
    cpu = os.cpu_count() or 4
    workers = max(1, min(MAX_WORKERS, cpu - 1))  # reserve one core

    avail = _available_ram_bytes()
    if avail:
        frame_bytes = TARGET_SIZE[0] * TARGET_SIZE[1] * 3
        # ~3.5x fudge over raw frame bytes (numpy copies + ImageClip objects), measured.
        timeline = int(max(0, n_photos) * frame_bytes * 3.5)  # loaded ONCE, spread across workers
        budget = int(avail * WORKER_RAM_BUDGET)
        # timeline + workers*overhead <= budget  ->  workers <= (budget - timeline)/overhead
        mem_workers = (budget - timeline) // _WORKER_BASE_BYTES
        workers = min(workers, max(1, mem_workers))
    return int(workers)


def _render_final_parallel(specs, audio, durations, motion, photo_seconds,
                           output_path, progress_cb, n_workers):
    """Full-quality path: encode the timeline across `n_workers` cores, then concat +
    mux. Each worker gets only its slice of specs, so it decodes only that slice's
    photos - all workers together hold ~one timeline, not one per worker."""
    bounds = _chunk_boundaries(durations, n_workers)
    n = len(bounds)
    tmpdir = Path(tempfile.mkdtemp(prefix="tripvid_"))
    try:
        tasks = [
            (k, specs[s:e], motion, photo_seconds, 1.0, str(tmpdir / f"seg_{k:03d}.mp4"), "veryfast")
            for k, (s, e) in enumerate(bounds)
        ]
        # Progress counts SEGMENTS finished, not cores or frames. The n parts are
        # balanced to take ~equal time, so the count tends to stay low and then jump
        # to n/n near the end - that's expected, not a stall (hence the wording).
        msg = f"Rendering {n} parts across {n} cores"
        if progress_cb is not None:
            progress_cb(0, n, f"{msg}… (0/{n} parts done)")
        seg_files = [None] * n
        done = 0
        ctx = mp.get_context("spawn")
        with ctx.Pool(n) as pool:
            for k, path in pool.imap_unordered(_render_chunk, tasks):
                seg_files[k] = path
                done += 1
                if progress_cb is not None:
                    progress_cb(done, n, f"{msg}… ({done}/{n} parts done)")

        if progress_cb is not None:
            progress_cb(0, 0, "Adding the soundtrack…")
        audio_path = tmpdir / "audio.m4a"
        audio.write_audiofile(str(audio_path), fps=44100, codec="aac", logger=None)

        if progress_cb is not None:
            progress_cb(0, 0, "Combining the film…")
        _concat_and_mux(seg_files, audio_path, output_path)
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)


def generate_video(conn, progress_cb=None, mode: str = "final", basename: str | None = None,
                   render_parallel: bool | None = None) -> Path:
    """Render the film. mode='draft' is a fast, small-canvas single-pass preview;
    mode='final' renders full 1080p (the encode is producer-bound - single-threaded
    frame generation - so splitting it across processes is the real speedup).

    `basename` is the output filename stem; the caller passes the current project's
    name so each project's film is named after itself. Falls back to the global
    output_basename setting when not given (e.g. a CLI/legacy caller).

    `render_parallel` chooses the final-render path: True fans the encode out across
    CPU cores (faster, more CPU/RAM), False renders single-process (memory-safe on
    constrained machines). This is a STANDARD feature - the user picks it per render
    on the Preview page. When None, falls back to the saved 'render_parallel'
    preference (default on). Draft always renders single-pass regardless."""
    draft = mode == "draft"
    scale = DRAFT_SCALE if draft else 1.0

    _t_build_start = time.perf_counter()
    _apply_render_scale(scale)
    try:
        render_size = TARGET_SIZE  # captured before the finally resets it to 1080p
        specs, audio_segments, motion, photo_seconds, n_photos, n_clips = _plan(conn, progress_cb)

        if progress_cb is not None:
            progress_cb(0, 0, "Adding titles and music…")
        _t_assemble_start = time.perf_counter()

        durations = [seg[0] for seg in audio_segments]
        total_duration = sum(durations)
        audio = _build_audio_track(audio_segments, total_duration)

        output_dir = Path(get_setting("output_dir", str(OUTPUT_DIR)))
        if basename is None:
            basename = get_setting("output_basename", "trip_video")
        output_dir.mkdir(parents=True, exist_ok=True)
        suffix = "_draft" if draft else ""
        stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        output_path = output_dir / f"{basename}{suffix}_{stamp}.mp4"

        # Draft is one fast pass on the small canvas. For the final render the user
        # chooses single vs. multiprocessor (a standard feature); when None, use the
        # saved preference (default on). Multiprocessor fans out across as many cores
        # as fit CPU headroom AND a RAM budget - if that's just one, render in-process.
        if render_parallel is None:
            render_parallel = get_bool("render_parallel", True)
        parallel = (not draft) and render_parallel
        n_workers = _plan_workers(n_photos) if parallel else 1
        if parallel:
            _free = _available_ram_bytes()
            print(
                f"render plan: {n_workers} worker(s) "
                f"(cores={os.cpu_count()}, free RAM"
                f"{f'≈{_free / 1024**3:.1f} GB' if _free else '=unknown'}, "
                f"{n_photos} photos)",
                flush=True,
            )

        _t_encode_start = time.perf_counter()
        if draft or n_workers <= 1:
            # Single in-process pass: build every spec here (one process holds the whole
            # timeline) and encode. The memory-safe path on constrained machines.
            preset = "ultrafast" if draft else "veryfast"
            clips = [_build_spec(s, motion, photo_seconds) for s in specs]
            video = concatenate_videoclips(clips, method="chain").with_audio(audio)
            logger = _EncodeProgressLogger(progress_cb) if progress_cb is not None else "bar"
            video.write_videofile(
                str(output_path), fps=FPS, codec="libx264", audio_codec="aac",
                preset=preset, ffmpeg_params=_keyframe_params(_cut_times(durations)),
                logger=logger,
            )
        else:
            # Fan out: each worker builds only its slice's photos (bounded memory).
            _render_final_parallel(specs, audio, durations, motion, photo_seconds,
                                   output_path, progress_cb, n_workers)
        _t_end = time.perf_counter()
    finally:
        _apply_render_scale(1.0)  # never leave the module in draft geometry

    build_s = _t_assemble_start - _t_build_start
    assemble_s = _t_encode_start - _t_assemble_start
    encode_s = _t_end - _t_encode_start
    total_s = _t_end - _t_build_start

    def _mmss(s: float) -> str:
        return f"{int(s) // 60:d}m{int(s) % 60:02d}s"

    print(
        "\n===== render timing =====\n"
        f"  mode           : {mode}  ({render_size[0]}x{render_size[1]})\n"
        f"  items          : {n_photos} photos + {n_clips} clips, "
        f"{total_duration:.1f}s video, motion={motion}\n"
        f"  1) build clips : {_mmss(build_s)}  ({build_s:6.1f}s)\n"
        f"  2) assemble a/v: {_mmss(assemble_s)}  ({assemble_s:6.1f}s)\n"
        f"  3) encode video: {_mmss(encode_s)}  ({encode_s:6.1f}s)\n"
        f"  TOTAL          : {_mmss(total_s)}  ({total_s:6.1f}s)\n"
        "=========================\n",
        flush=True,
    )
    return output_path
