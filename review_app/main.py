"""FastAPI review server (Premise 14: 127.0.0.1-only, no auth needed since
nothing outside the machine can reach it)."""

from urllib.parse import urlparse

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.middleware.trustedhost import TrustedHostMiddleware
from starlette.responses import PlainTextResponse

from db.schema import init_db
from review_app.routes import review, consent, faces, setup, hub, lang, logs

# Timestamped uvicorn logging that also persists to data/logs/tripvideo.log.
# Kept importable under this name for manage.py / app_main.py.
from runtime_log import build_log_config  # noqa: F401

# Ensure any additive schema migrations (e.g. trip_intro.outro_text) are
# applied for pre-existing DBs - `serve` never calls init_db otherwise.
init_db()

app = FastAPI(title="Trip Video Review")

# The server is loopback-only and unauthenticated (Premise 14). That stops
# network peers, but not a browser on this machine acting as a confused deputy
# for a malicious web page. Two such vectors are closed here:
#
#   * DNS rebinding (cross-origin *reads* of the local filesystem browser and
#     imported media) - defeated by pinning the Host header. A rebound request
#     still arrives as `Host: evil.com`, which the allow-list rejects; a page
#     cannot make the browser send `Host: localhost` for its own domain.
#   * CSRF (cross-site *writes*: delete music/projects, trigger imports) -
#     defeated by requiring same-origin intent on state-changing methods.
_LOCAL_HOSTS = ["localhost", "127.0.0.1"]  # Host is matched without its port
_SAFE_METHODS = {"GET", "HEAD", "OPTIONS"}


class SameOriginWriteMiddleware(BaseHTTPMiddleware):
    """Block cross-site POST/PUT/PATCH/DELETE. Browsers set Sec-Fetch-Site
    automatically and a foreign page cannot forge it; we fall back to the Origin
    host for pre-Sec-Fetch clients. The app's own UI uses same-origin fetch()
    and top-level navigations report 'none', so both are allowed."""

    async def dispatch(self, request, call_next):
        if request.method not in _SAFE_METHODS:
            site = request.headers.get("sec-fetch-site")
            if site is not None:
                if site not in ("same-origin", "none"):
                    return PlainTextResponse("cross-site request blocked", status_code=403)
            else:
                origin = request.headers.get("origin")
                if origin and urlparse(origin).hostname not in _LOCAL_HOSTS:
                    return PlainTextResponse("cross-site request blocked", status_code=403)
        return await call_next(request)


app.add_middleware(SameOriginWriteMiddleware)
app.add_middleware(TrustedHostMiddleware, allowed_hosts=_LOCAL_HOSTS)

app.include_router(hub.router)
app.include_router(lang.router)
app.include_router(review.router)
app.include_router(consent.router)
app.include_router(faces.router)
app.include_router(setup.router)
app.include_router(logs.router)

from paths import resource_path

_static_dir = resource_path("review_app/static")
if _static_dir.exists():
    app.mount("/static", StaticFiles(directory=str(_static_dir)), name="static")


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="127.0.0.1", port=8000, log_config=build_log_config())
