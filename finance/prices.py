"""Warm and refresh the fund-price cache the optimizer reads from.

The optimizer never downloads prices while a tab is open; instead the whole set of tickers (every account's
latest holdings, every fund-menu symbol, and the comparison benchmarks) is fetched in one batched download:
once when the app starts and again every day at a quiet hour. A ticker with no cached history is backfilled
with ten years; the rest only have recent days topped up. See `benchmarks.refresh_fund_prices`.
"""
from __future__ import annotations

import logging

from finance import benchmarks as bm
from finance import fundmenu as fm
from finance import services
from finance.config import Settings, get_settings

log = logging.getLogger(__name__)


def tracked_symbols(settings: Settings | None = None) -> list[str]:
    """All tickers to keep priced: account holdings + fund menus + the benchmarks."""
    return sorted(set(services.read(fm.tracked_symbols)) | set(bm.BENCHMARKS))


def refresh(settings: Settings | None = None) -> dict[str, str]:
    """Fetch/refresh adjusted closes for every tracked ticker in one batched download. Returns {symbol: error}."""
    settings = settings or get_settings()
    symbols = tracked_symbols(settings)
    if not symbols:
        log.info("Fund-price refresh: no tickers to price yet")
        return {}
    errors = services.write(bm.refresh_fund_prices, symbols, settings.demo)
    ok = len([s for s in symbols if s not in errors])
    if errors:
        log.info("Fund-price refresh: %d ticker(s) up to date, %d without data (%s)",
                 ok, len(errors), ", ".join(sorted(errors)[:10]))
    else:
        log.info("Fund-price refresh: %d ticker(s) up to date", ok)
    return errors
