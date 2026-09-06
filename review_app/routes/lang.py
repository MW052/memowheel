"""Language switch: GET /lang/{code} sets the `lang` cookie and returns to the
page the user came from (the globe toggle in the nav links here). A GET with a
redirect is fine here - switching language is idempotent and has no side effects
beyond the cookie, and it keeps the toggle a plain link (works without JS)."""

from fastapi import APIRouter, Request
from fastapi.responses import RedirectResponse

from review_app.i18n import LANGS, DEFAULT_LANG

router = APIRouter()

_ONE_YEAR = 60 * 60 * 24 * 365


@router.get("/lang/{code}")
def set_lang(code: str, request: Request):
    code = code if code in LANGS else DEFAULT_LANG
    back = request.headers.get("referer") or "/"
    resp = RedirectResponse(back, status_code=303)
    resp.set_cookie("lang", code, max_age=_ONE_YEAR, samesite="lax", httponly=False)
    return resp
