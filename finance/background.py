"""Run SimpleFIN syncs in a background thread so the web UI stays responsive."""
from __future__ import annotations

import datetime as dt
import logging
import os
import threading
from dataclasses import dataclass

from finance.config import Settings, get_settings
from finance.models import utcnow
from finance.sync import SyncResult, run_sync

log = logging.getLogger(__name__)

DEFAULT_MAX_AGE_HOURS = 12.0


@dataclass
class SyncState:
    running: bool = False
    started_at: dt.datetime | None = None
    finished_at: dt.datetime | None = None
    result: SyncResult | None = None
    trigger: str | None = None  # manual | startup | full
    version: int = 0            # increments after every finished sync (pages refresh on change)


class SyncManager:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self.state = SyncState()
        self._scheduler = None  # APScheduler BackgroundScheduler, created on first schedule_daily

    def can_sync(self, settings: Settings | None = None) -> tuple[bool, str]:
        settings = settings or get_settings()
        if settings.demo or settings.simplefin_access_url:
            return True, ""
        return False, "SIMPLEFIN_ACCESS_URL is not set in .env"

    def start(self, full: bool = False, trigger: str = "manual", settings: Settings | None = None) -> bool:
        settings = settings or get_settings()
        ok, _ = self.can_sync(settings)
        if not ok:
            return False
        with self._lock:
            if self.state.running:
                return False
            self.state.running = True
            self.state.started_at = utcnow()
            self.state.trigger = "full" if full else trigger
        threading.Thread(target=self._run, args=(full, settings), daemon=True, name="simplefin-sync").start()
        return True

    def _run(self, full: bool, settings: Settings) -> None:
        log.info("Sync started (%s)", "full" if full else self.state.trigger)
        try:
            result = run_sync(settings=settings, full=full)
            log.info("Sync finished: status=%s, %d request(s), %d new, %d updated",
                     result.status, getattr(result, "requests", 0) or 0,
                     getattr(result, "new", 0) or 0, getattr(result, "updated", 0) or 0)
        except Exception as exc:  # run_sync already logs; never kill the thread silently
            log.exception("Background sync failed")
            result = SyncResult(status="error", mode="full" if full else "?", errors=[str(exc)])
        with self._lock:
            self.state.result = result
            self.state.finished_at = utcnow()
            self.state.running = False
            self.state.version += 1

    def start_if_stale(self, settings: Settings | None = None) -> bool:
        """Startup sync: only if the last successful sync is older than FINANCE_SYNC_MAX_AGE_HOURS."""
        from finance.services import last_sync

        settings = settings or get_settings()
        max_age = float(os.getenv("FINANCE_SYNC_MAX_AGE_HOURS", DEFAULT_MAX_AGE_HOURS))
        last = last_sync(settings, ok_only=True)
        if last and last.finished_at and utcnow() - last.finished_at < dt.timedelta(hours=max_age):
            return False
        return self.start(trigger="startup", settings=settings)

    def schedule_daily(self, hour: int, minute: int = 0, settings: Settings | None = None):
        """Run a sync once a day at ``hour``:``minute`` (local time) via APScheduler.

        Used by the Home Assistant add-on. SimpleFIN allows only ~24 requests/day per
        connection, so once daily is safe. Returns the scheduler, or ``None`` if syncing is
        not possible (no credentials). Replaces any previously scheduled daily job.
        """
        from apscheduler.schedulers.background import BackgroundScheduler

        settings = settings or get_settings()
        ok, reason = self.can_sync(settings)
        if not ok:
            log.warning("Daily sync not scheduled: %s", reason)
            return None
        if self._scheduler is None:
            self._scheduler = BackgroundScheduler(timezone=settings.timezone, daemon=True)
            self._scheduler.start()
        self._scheduler.add_job(
            lambda: self.start(trigger="scheduled", settings=settings),
            trigger="cron", hour=hour, minute=minute, id="daily-sync", replace_existing=True,
        )
        log.info("Daily sync scheduled for %02d:%02d %s", hour, minute, settings.timezone)
        return self._scheduler


sync_manager = SyncManager()
