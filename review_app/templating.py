"""Shared Jinja2Templates instance, wired with context processors so every page
gets `t`, `lang`, `lang_dir`, `LANGS`, `i18n_json` (i18n) plus `app_name` and
`trip_name` (the product chrome) automatically - no per-route plumbing. All
routes import `templates` from here.
"""

import re

from fastapi.templating import Jinja2Templates

import config
from features import APP_NAME, APP_VERSION
from paths import resource_path
from review_app.i18n import i18n_context


def app_context(request) -> dict:
    """Brand + current project name for the shared product header. Prefer the
    active project's name; fall back to the output basename for a lone catalog
    with no project registered yet."""
    import project_store

    proj = project_store.current()
    if proj:
        trip_name = proj["name"]
    else:
        basename = (config.get_setting("output_basename", "trip") or "trip").strip()
        trip_name = basename.replace("_", " ").replace("-", " ").title() or "Trip"
    kind = project_store.project_kind(proj["id"]) if proj else "trip"
    return {"app_name": APP_NAME, "app_version": APP_VERSION, "trip_name": trip_name,
            "project_kind": kind}


def asset_ver(filename: str) -> int:
    """Cache-buster for /static assets: the file's mtime. Appended as ?v=... so
    the browser refetches the moment we edit a stylesheet/script instead of
    serving an indefinitely-cached copy. Missing file -> 0 (link still works)."""
    try:
        return int((resource_path("review_app/static") / filename).stat().st_mtime)
    except OSError:
        return 0


_TRACK_NUM_RE = re.compile(r"^\s*\d{1,3}\s*[._)\-]+\s*")


def humanize_track(name: str) -> str:
    """Turn a raw music filename into a readable label for display only.

    Strips the extension and a leading track-number prefix ("01 ", "01_",
    "03 - ", …), turns _/- separators into spaces, collapses whitespace and
    gently Title-Cases the result. The real filename is still what every
    action posts (templates keep it in data-name), so humanizing is cosmetic
    and reversible. Hebrew and other non-Latin text pass through unchanged.
    Falls back to the original stem if cleaning would empty the label.
    """
    if not name:
        return ""
    stem = name.rsplit(".", 1)[0] if "." in name else name
    cleaned = _TRACK_NUM_RE.sub("", stem)
    cleaned = re.sub(r"[_\-]+", " ", cleaned)
    cleaned = re.sub(r"\s+", " ", cleaned).strip()
    if not cleaned:
        return stem.strip() or name
    # Title-case only all-lowercase ASCII words so acronyms ("DJ") and
    # already-cased or Hebrew words are left as the user named them.
    def _cap(word: str) -> str:
        return word[:1].upper() + word[1:] if word.isascii() and word.islower() else word
    return " ".join(_cap(w) for w in cleaned.split(" "))


def safe_filename(name: str, default: str = "video") -> str:
    """A download-friendly base filename from a project name (BL-25): keep
    letters/numbers/spaces/dashes (any script, so Hebrew names survive), drop
    characters a filesystem dislikes, collapse whitespace. No extension here —
    the caller appends it. Falls back to `default` if nothing usable remains."""
    cleaned = re.sub(r'[\\/:*?"<>|\x00-\x1f]+', " ", name or "")
    cleaned = re.sub(r"\s+", " ", cleaned).strip(" .")
    return cleaned or default


templates = Jinja2Templates(
    directory=str(resource_path("review_app/templates")),
    context_processors=[i18n_context, app_context],
)
templates.env.globals["asset_ver"] = asset_ver
templates.env.filters["humanize_track"] = humanize_track
templates.env.filters["safe_filename"] = safe_filename
