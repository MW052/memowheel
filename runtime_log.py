"""Runtime logging: persist the server's logs to a file on the box so there's a
record even when the app runs windowless (system-tray / pythonw, no console).

`LOG_FILE` lives under the writable `data/` dir (cwd-relative, like the rest of
the app's user data; the frozen entry point chdir's to the app folder). Logs
rotate so they never grow without bound.

Three entry points use this:
- `manage.py serve` (dev): console + file (`build_log_config()` with console on).
- the tray app (windowless): file only, plus `redirect_streams_to_log()` so
  stray `print()`/tracebacks are captured too (pythonw has no real stdout).
- `app_main.py` (frozen): same as the tray path when packaged windowless.
"""

import copy
import logging
import logging.handlers
import sys
from pathlib import Path

LOG_DIR = Path("data") / "logs"
LOG_FILE = LOG_DIR / "tripvideo.log"

_MAX_BYTES = 1_000_000  # ~1 MB per file
_BACKUPS = 3            # tripvideo.log + .1 .2 .3


def build_log_config(console: bool = True) -> dict:
    """Uvicorn `log_config` dict that timestamps every line AND tees to
    `LOG_FILE` (rotating). With `console=False` the terminal handlers are
    dropped (for windowless runs where sys.stdout may be None), leaving only the
    file handler. Access-log lines render through the same plain file formatter
    via the record's own message, so no colour codes reach the file."""
    from uvicorn.config import LOGGING_CONFIG

    config = copy.deepcopy(LOGGING_CONFIG)
    # Console formatters: prepend an ISO-ish timestamp (uvicorn omits it).
    config["formatters"]["default"]["fmt"] = "%(asctime)s %(levelprefix)s %(message)s"
    config["formatters"]["access"]["fmt"] = (
        '%(asctime)s %(levelprefix)s %(client_addr)s - '
        '"%(request_line)s" %(status_code)s'
    )
    for formatter in config["formatters"].values():
        formatter["datefmt"] = "%Y-%m-%d %H:%M:%S"

    # File handler (plain, no ANSI): shared by every logger so one file holds the
    # whole story. A single handler instance avoids rotation races on Windows.
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    config["formatters"]["file"] = {
        "format": "%(asctime)s %(levelname)s %(name)s %(message)s",
        "datefmt": "%Y-%m-%d %H:%M:%S",
    }
    config["handlers"]["file"] = {
        "class": "logging.handlers.RotatingFileHandler",
        "filename": str(LOG_FILE),
        "maxBytes": _MAX_BYTES,
        "backupCount": _BACKUPS,
        "formatter": "file",
        "encoding": "utf-8",
    }

    # Only the two loggers that own handlers + don't propagate. `uvicorn.error`
    # propagates to `uvicorn`, so attaching "file" there too would double every
    # startup/error line in the file.
    for name in ("uvicorn", "uvicorn.access"):
        logger = config["loggers"].get(name)
        if logger is None:
            continue
        handlers = list(logger.get("handlers", []))
        if not console:
            handlers = [h for h in handlers if h not in ("default", "access")]
        if "file" not in handlers:
            handlers.append("file")
        logger["handlers"] = handlers

    # Root catches app-level logging and the redirected stdout/stderr streams.
    root = config.setdefault("root", {"level": "INFO", "handlers": []})
    root["level"] = "INFO"
    root_handlers = list(root.get("handlers", []))
    if "file" not in root_handlers:
        root_handlers.append("file")
    root["handlers"] = root_handlers

    return config


class _StreamToLogger:
    """Minimal file-like object that forwards writes to a logger, one line per
    record. Lets us capture `print()` and uncaught tracebacks into the log file
    when running windowless (where sys.stdout/err are otherwise None)."""

    def __init__(self, logger: logging.Logger, level: int):
        self._logger = logger
        self._level = level
        self._buffer = ""

    def write(self, message: str) -> int:
        self._buffer += message
        while "\n" in self._buffer:
            line, self._buffer = self._buffer.split("\n", 1)
            if line.strip():
                self._logger.log(self._level, line)
        return len(message)

    def flush(self) -> None:
        if self._buffer.strip():
            self._logger.log(self._level, self._buffer)
        self._buffer = ""

    def isatty(self) -> bool:
        return False


def redirect_streams_to_log() -> None:
    """Point sys.stdout/stderr at the log file (via the logging system) so a
    windowless process still records prints and tracebacks. Safe to call once at
    startup; only meaningful together with build_log_config(console=False)."""
    sys.stdout = _StreamToLogger(logging.getLogger("app.stdout"), logging.INFO)
    sys.stderr = _StreamToLogger(logging.getLogger("app.stderr"), logging.ERROR)
