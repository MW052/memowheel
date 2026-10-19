"""Feature registry for the home hub.

This app is a hub of "makers". Adding a new major feature (occasion videos, face
morph, ...) is: (1) add an entry here with status "available" and its entry URL,
and (2) add its routes/templates. The home page (`/`) renders these cards; the
nav and everything else stays the same.

APP_NAME is the hub's name - change this one string to rebrand.
"""

import paths

APP_NAME = "Memowheel"
APP_TAGLINE = "Make something to remember."


def _read_version() -> str:
    """The app's version string, from the bundled VERSION file (single source of
    truth, also used by the release tag). Empty if it can't be read."""
    try:
        return paths.resource_path("VERSION").read_text(encoding="utf-8").strip()
    except Exception:
        return ""


APP_VERSION = _read_version()

FEATURES = [
    {
        "key": "trip",
        "name": "Trip Video",
        "emoji": "🧳",
        "desc": "Turn a trip's photos and clips into a keepsake movie.",
        "status": "available",
        "url": "/trip",
    },
    {
        "key": "occasion",
        "name": "Occasion Video",
        "emoji": "🎂",
        "desc": "A video from photos for a birthday, anniversary, or memorial.",
        "status": "soon",
        "url": None,
    },
    # Face Morph is being developed separately - intentionally not shown on the hub.
]
