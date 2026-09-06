"""Home hub: the app's front door (`/`), a menu of makers (feature registry)."""

from pathlib import Path

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse

from features import APP_NAME, APP_TAGLINE, FEATURES
from review_app.templating import templates

router = APIRouter()


@router.get("/", response_class=HTMLResponse)
def home(request: Request):
    return templates.TemplateResponse(request, "home.html", {
        "app_name": APP_NAME,
        "app_tagline": APP_TAGLINE,
        "features": FEATURES,
    })
