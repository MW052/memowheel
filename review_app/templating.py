"""Shared Jinja2Templates instance, wired with context processors so every page
gets `t`, `lang`, `lang_dir`, `LANGS`, `i18n_json` (i18n) plus `app_name` and
`trip_name` (the product chrome) automatically - no per-route plumbing. All
routes import `templates` from here.
"""

from fastapi.templating import Jinja2Templates

import config
from features import APP_NAME
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
    return {"app_name": APP_NAME, "trip_name": trip_name}


def asset_ver(filename: str) -> int:
    """Cache-buster for /static assets: the file's mtime. Appended as ?v=... so
    the browser refetches the moment we edit a stylesheet/script instead of
    serving an indefinitely-cached copy. Missing file -> 0 (link still works)."""
    try:
        return int((resource_path("review_app/static") / filename).stat().st_mtime)
    except OSError:
        return 0


templates = Jinja2Templates(
    directory=str(resource_path("review_app/templates")),
    context_processors=[i18n_context, app_context],
)
templates.env.globals["asset_ver"] = asset_ver
