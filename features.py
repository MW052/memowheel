"""Feature registry for the home hub.

This app is a hub of "makers". Adding a new major feature (occasion videos, face
morph, ...) is: (1) add an entry here with status "available" and its entry URL,
and (2) add its routes/templates. The home page (`/`) renders these cards; the
nav and everything else stays the same.

APP_NAME is the hub's name - change this one string to rebrand.
"""

APP_NAME = "Memowheel"
APP_TAGLINE = "Make something to remember."

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
