"""Container metadata probing via the bundled ffmpeg (imageio-ffmpeg).

These reads used to shell out to `ffprobe`. The packaged (PyInstaller) app
bundles imageio-ffmpeg's `ffmpeg` binary but NOT `ffprobe`, so on a clean
machine (no system ffmpeg on PATH) any direct `ffprobe` call fails with
FileNotFoundError - which broke clip import. MoviePy's `ffmpeg_parse_infos`
reads the same container metadata using that one bundled `ffmpeg` binary, so we
route all probing through it: one native dependency for both probing and
rendering, and nothing extra to ship.

`ffmpeg_parse_infos` raises OSError when ffmpeg itself can't read the file;
callers translate that into the pipeline's UnreadableFileError (skip + log).

moviepy is imported lazily inside `probe_infos` (not at module load): it pulls
in the whole heavy video/imageio stack, and this module is imported at app
startup via the ingest pipeline. Deferring the import keeps startup light and
means a moviepy/imageio problem degrades clip probing rather than preventing the
app from launching at all.
"""


def probe_infos(path: str) -> dict:
    # decode_file=False: parse the container header only (fast, ffprobe-like),
    # no full frame decode.
    from moviepy.video.io.ffmpeg_reader import ffmpeg_parse_infos

    return ffmpeg_parse_infos(path, decode_file=False)


def probe_duration_and_audio(path: str) -> tuple[float, bool]:
    """(duration_seconds, has_audio_stream). Raises OSError/KeyError/ValueError
    if the container can't be read or reports no duration."""
    infos = probe_infos(path)
    return float(infos["duration"]), bool(infos.get("audio_found", False))


def probe_creation_time(path: str) -> str | None:
    """The container's `creation_time` tag, or None if absent. Raises OSError
    if ffmpeg can't read the file."""
    return probe_infos(path).get("metadata", {}).get("creation_time")
