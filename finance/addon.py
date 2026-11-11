"""Home Assistant add-on integration: options, ingress path, daily-sync config.

Everything here is active only when the app runs inside the HA Supervisor (``SUPERVISOR_TOKEN``
is set in the environment). For a normal local run every function no-ops or returns ``None``,
so importing and calling this module is always safe.

Flow inside the add-on:
  * :func:`apply_options` copies ``/data/options.json`` (what the user typed in the add-on's
    HA configuration) into the environment variables the rest of the app already reads.
  * :func:`ingress_prefix` asks the Supervisor for this add-on's ingress base path and becomes
    Dash's ``requests_pathname_prefix`` so assets/callbacks resolve behind HA ingress.
"""
from __future__ import annotations

import json
import logging
import os
from pathlib import Path

log = logging.getLogger(__name__)

OPTIONS_FILE = Path("/data/options.json")
SUPERVISOR_URL = "http://supervisor"

# add-on option name (config.yaml schema) -> environment variable the app already understands
_OPTION_ENV = {
    "simplefin_access_url": "SIMPLEFIN_ACCESS_URL",
    "timezone": "FINANCE_TIMEZONE",
    "daily_sync_hour": "FINANCE_DAILY_SYNC_HOUR",
    "sync_max_age_hours": "FINANCE_SYNC_MAX_AGE_HOURS",
}


def in_addon() -> bool:
    """True when running inside the Home Assistant Supervisor."""
    return bool(os.getenv("SUPERVISOR_TOKEN"))


def apply_options(path: Path = OPTIONS_FILE) -> dict:
    """Copy HA add-on options into ``os.environ`` and return the parsed options.

    An environment variable already set wins over the file, so a manual run or a test can
    still override. Missing/empty options are skipped (the app keeps its own defaults).
    """
    try:
        opts = json.loads(path.read_text("utf-8"))
    except (OSError, ValueError):
        return {}
    if not isinstance(opts, dict):
        return {}
    for key, env in _OPTION_ENV.items():
        val = opts.get(key)
        if val not in (None, "") and not os.environ.get(env):
            os.environ[env] = str(val)
    return opts


def normalize_prefix(entry: str | None) -> str | None:
    """'/api/hassio_ingress/<token>' -> '/api/hassio_ingress/<token>/' (Dash needs both slashes)."""
    if not entry or not entry.strip("/"):
        return None
    return "/" + entry.strip("/") + "/"


def ingress_prefix(timeout: float = 5.0) -> str | None:
    """This add-on's ingress base path from the Supervisor, or ``None`` if unavailable.

    HA ingress strips this prefix before proxying to us (so our routes stay mounted at ``/``),
    but the browser must request under it - hence it becomes Dash's ``requests_pathname_prefix``.
    Any failure (not in an add-on, no API access, network error) returns ``None`` so the app
    still starts; it is simply reachable only by direct port in that case.
    """
    token = os.getenv("SUPERVISOR_TOKEN")
    if not token:
        return None
    try:
        import requests

        r = requests.get(f"{SUPERVISOR_URL}/addons/self/info",
                         headers={"Authorization": f"Bearer {token}"}, timeout=timeout)
        r.raise_for_status()
        entry = (r.json().get("data") or {}).get("ingress_entry")
    except Exception as exc:  # pragma: no cover - network/permission paths, never fatal
        log.warning("Could not read ingress path from Supervisor (%s); direct port only", exc)
        return None
    prefix = normalize_prefix(entry)
    if prefix:
        log.info("Serving behind Home Assistant ingress at %s", prefix)
    return prefix
