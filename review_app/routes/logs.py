"""Log viewer: shows the server's own log file (data/logs/tripvideo.log) so a
user running the app windowless (system tray, no console) can still see what it
is doing. Reached from the kebab menu's "View log" item.
"""

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, PlainTextResponse

from runtime_log import LOG_FILE
from review_app.templating import templates

router = APIRouter()

_TAIL_BYTES = 200_000  # show the last ~200 KB; the full file is the rotating log


def _read_tail() -> str:
    """Last _TAIL_BYTES of the log as text (whole file if smaller). Empty string
    if the log doesn't exist yet."""
    try:
        size = LOG_FILE.stat().st_size
        with LOG_FILE.open("r", encoding="utf-8", errors="replace") as fh:
            if size > _TAIL_BYTES:
                fh.seek(size - _TAIL_BYTES)
                fh.readline()  # drop the partial first line after the seek
            return fh.read()
    except OSError:
        return ""


@router.get("/logs", response_class=HTMLResponse)
def view_log(request: Request):
    return templates.TemplateResponse(request, "logs.html", {
        "log_text": _read_tail(),
        "log_path": str(LOG_FILE),
    })


@router.get("/logs/raw", response_class=PlainTextResponse)
def raw_log():
    """Plain-text tail — used by the viewer's auto-refresh and as a download."""
    return _read_tail() or "(no log yet)"
