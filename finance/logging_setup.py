"""Application logging: a rotating file in the data dir plus the console.

The file lives at ``<data_dir>/logs/finance.log`` so it is persistent (on the
Home Assistant add-on, ``/data`` survives restarts). ``configure_logging`` is
idempotent, so it is safe to call from both the CLI and the web app startup.
"""
from __future__ import annotations

import logging
import logging.handlers

from finance.config import Settings, get_settings

_FORMAT = "%(asctime)s %(levelname)s %(name)s: %(message)s"
_DATEFMT = "%Y-%m-%d %H:%M:%S"
_configured = False


def _is_console(h: logging.Handler) -> bool:
    return isinstance(h, logging.StreamHandler) and not isinstance(h, logging.FileHandler)


def configure_logging(settings: Settings | None = None, level: int = logging.INFO) -> None:
    """Send INFO+ logs to the console and to a rotating finance.log. Idempotent."""
    global _configured
    if _configured:
        return
    settings = settings or get_settings()
    fmt = logging.Formatter(_FORMAT, _DATEFMT)
    root = logging.getLogger()
    root.setLevel(level)

    # Console: reuse an existing stream handler (e.g. one basicConfig added) or add one.
    console = next((h for h in root.handlers if _is_console(h)), None)
    if console is None:
        console = logging.StreamHandler()
        root.addHandler(console)
    console.setFormatter(fmt)

    # Rotating file handler (5 x 2 MB). A failure here must not stop the app.
    try:
        settings.log_dir.mkdir(parents=True, exist_ok=True)
        fh = logging.handlers.RotatingFileHandler(
            settings.log_file, maxBytes=2_000_000, backupCount=5, encoding="utf-8")
        fh.setFormatter(fmt)
        root.addHandler(fh)
    except OSError as exc:  # pragma: no cover - depends on the filesystem
        root.warning("Could not open log file %s: %s", settings.log_file, exc)

    _configured = True
    logging.getLogger(__name__).info("Logging to %s", settings.log_file)
