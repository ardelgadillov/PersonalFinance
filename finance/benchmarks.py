"""Benchmark prices (e.g. S&P 500 via SPY) for performance comparisons.

Daily closes are fetched from Yahoo Finance's public chart endpoint (only the ticker and a date range are
sent) and cached in `benchmark_prices`. A symbol is re-fetched at most every REFETCH_HOURS per process, and
only when the cache does not cover the requested range. Demo mode uses a deterministic fake series and never
touches the network.

Returns are price returns (close to close, dividends not included), the same basis as the holdings' prices.

Fund prices for the optimizer (`fund_prices`) use the dividend-adjusted close instead (total return), like the
original Markowitz notebook (yfinance 'Adj Close').
"""
from __future__ import annotations

import datetime as dt
import logging
import random
import time
from collections.abc import Callable

import pandas as pd
import yfinance as yf
from sqlalchemy import func, select
from sqlalchemy.dialects.sqlite import insert
from sqlalchemy.orm import Session

from finance.models import BenchmarkPrice, FundPrice

BENCHMARKS = {
    "SPY": "S&P 500 (SPY)",
    "VTI": "US total market (VTI)",
    "QQQ": "Nasdaq-100 (QQQ)",
    "VT": "World stocks (VT)",
    "AGG": "US bonds (AGG)",
}
DEFAULT = "SPY"
REFETCH_HOURS = 6

log = logging.getLogger(__name__)

Fetcher = Callable[[str, dt.date, dt.date], list[tuple[dt.date, float]]]
_last_attempt: dict[str, float] = {}


class BenchmarkError(RuntimeError):
    pass


def _yahoo_chart(symbol: str, start: dt.date, end: dt.date, adjusted: bool) -> list[tuple[dt.date, float]]:
    """Daily closes via the yfinance library (it wraps Yahoo Finance, handling the request format, crumbs and
    rate limiting, so we don't track Yahoo's endpoints ourselves).

    yfinance uses curl_cffi under the hood, which verifies TLS through the OS trust store and so works behind
    this machine's TLS-inspecting proxy without the Python 3.13 VERIFY_X509_STRICT workaround requests needed.
    """
    column = "Adj Close" if adjusted else "Close"
    try:
        # yfinance treats `end` as exclusive, so add a day to include closes on `end` itself.
        hist = yf.Ticker(symbol).history(start=start, end=end + dt.timedelta(days=1), interval="1d",
                                         auto_adjust=False, actions=False, raise_errors=False)
    except Exception as exc:  # noqa: BLE001 - yfinance raises assorted network/parse errors; funnel them
        raise BenchmarkError(f"Could not download {symbol} prices: {exc}") from exc
    if hist is None or hist.empty or column not in hist.columns:
        return []  # unknown symbol or no trading days in range; caller reports "no prices"
    out = []
    for ts, close in hist[column].items():
        if pd.notna(close):  # ts is tz-aware in the exchange's timezone; its date is the trading date
            out.append((ts.date(), round(float(close), 4)))
    return out


def fetch_yahoo(symbol: str, start: dt.date, end: dt.date) -> list[tuple[dt.date, float]]:
    """Daily closes (price return)."""
    return _yahoo_chart(symbol, start, end, adjusted=False)


def fetch_yahoo_adjusted(symbol: str, start: dt.date, end: dt.date) -> list[tuple[dt.date, float]]:
    """Daily dividend-adjusted closes (total return)."""
    return _yahoo_chart(symbol, start, end, adjusted=True)


ManyFetcher = Callable[[list[str], dt.date, dt.date], dict[str, list[tuple[dt.date, float]]]]


def fetch_many_adjusted(symbols: list[str], start: dt.date, end: dt.date) -> dict[str, list[tuple[dt.date, float]]]:
    """Dividend-adjusted daily closes for several tickers in ONE download (yfinance batches the request),
    returned as {symbol: [(date, adj_close), ...]}. A symbol Yahoo does not know simply has no entry.

    One request for the whole fund menu is far gentler on Yahoo's rate limit than one request per ticker,
    which is what used to trip the HTTP 429 "Too Many Requests" errors."""
    symbols = [s for s in symbols if s]
    if not symbols:
        return {}
    try:
        # yfinance treats `end` as exclusive; add a day to include closes on `end` itself.
        data = yf.download(tickers=symbols, start=start, end=end + dt.timedelta(days=1), interval="1d",
                           auto_adjust=False, actions=False, group_by="ticker", threads=True, progress=False)
    except Exception as exc:  # noqa: BLE001 - yfinance raises assorted network/parse errors; funnel them
        raise BenchmarkError(f"Could not download prices: {exc}") from exc
    out: dict[str, list[tuple[dt.date, float]]] = {}
    if data is None or data.empty:
        return out
    multi = isinstance(data.columns, pd.MultiIndex)
    for sym in symbols:
        try:
            col = data[sym]["Adj Close"] if multi else data["Adj Close"]
        except (KeyError, IndexError):
            continue  # ticker not in the response (unknown symbol): reported as "no prices" by the caller
        rows = [(ts.date(), round(float(c), 4)) for ts, c in col.items() if pd.notna(c)]
        if rows:
            out[sym] = rows
    return out


def fake_many_adjusted(symbols: list[str], start: dt.date, end: dt.date) -> dict[str, list[tuple[dt.date, float]]]:
    """Deterministic batched fund prices (demo mode and tests): the fake per-symbol series, keyed by symbol."""
    return {s: fake_adjusted(s, start, end) for s in symbols if s}


def fake_prices(symbol: str, start: dt.date, end: dt.date) -> list[tuple[dt.date, float]]:
    """Deterministic random walk per symbol from 2024-01-01 (demo mode and tests)."""
    rng = random.Random(sum(ord(ch) * (i + 1) for i, ch in enumerate(symbol)))
    price, day, out = 470.0 if symbol == "SPY" else 100.0, dt.date(2024, 1, 1), []
    while day <= end:
        if day.weekday() < 5:
            price *= 1 + rng.gauss(0.10 / 252, 0.16 / 252 ** 0.5)
            if day >= start:
                out.append((day, round(price, 4)))
        day += dt.timedelta(days=1)
    return out


def _seed(text: str) -> int:
    return sum(ord(ch) * (i + 1) for i, ch in enumerate(text))


def fake_adjusted(symbol: str, start: dt.date, end: dt.date) -> list[tuple[dt.date, float]]:
    """Deterministic fund prices from 2012 with a shared market factor, so funds are realistically correlated
    (demo mode and tests). Symbols containing BND / BTL / AGG behave like bond funds."""
    market = random.Random(5)
    rng = random.Random(_seed(symbol))
    bond = any(k in symbol for k in ("BND", "BTL", "AGG"))
    beta = rng.uniform(0.05, 0.2) if bond else rng.uniform(0.7, 1.2)
    drift = rng.uniform(0.025, 0.04) if bond else rng.uniform(-0.01, 0.03)  # plus beta * market premium
    idio = rng.uniform(0.02, 0.04) if bond else rng.uniform(0.04, 0.10)
    price, day, out = rng.uniform(20, 200), dt.date(2012, 1, 2), []
    while day <= end:
        if day.weekday() < 5:
            m = market.gauss(0.08 / 252, 0.16 / 252 ** 0.5)
            price *= 1 + drift / 252 + beta * m + rng.gauss(0.0, idio / 252 ** 0.5)
            if day >= start:
                out.append((day, round(price, 4)))
        day += dt.timedelta(days=1)
    return out


def _last_business_day(day: dt.date) -> dt.date:
    while day.weekday() >= 5:
        day -= dt.timedelta(days=1)
    return day


def ensure(s: Session, symbol: str, start: dt.date, end: dt.date, fetch: Fetcher,
           now: float | None = None) -> str | None:
    """Make sure closes for [start, end] are cached; returns an error message or None. Commits nothing."""
    lo, hi = s.execute(select(func.min(BenchmarkPrice.date), func.max(BenchmarkPrice.date))
                       .where(BenchmarkPrice.symbol == symbol)).one()
    # the latest close is usually yesterday's (or Friday's); a few days of slack covers holidays
    if _covered(lo, hi, start, end):
        return None
    now = time.time() if now is None else now
    if symbol in _last_attempt and now - _last_attempt[symbol] < REFETCH_HOURS * 3600 and lo is not None:
        return None  # tried recently; use what is cached
    _last_attempt[symbol] = now
    try:
        rows = fetch(symbol, min(start, lo or start) - dt.timedelta(days=7), end)
    except BenchmarkError as exc:
        return str(exc)
    if rows:
        stmt = insert(BenchmarkPrice).values([{"symbol": symbol, "date": d, "close": c} for d, c in rows])
        s.execute(stmt.on_conflict_do_update(index_elements=["symbol", "date"],
                                             set_={"close": stmt.excluded.close}))
    return None if rows else f"No {symbol} prices returned for this period."


def _covered(lo: dt.date | None, hi: dt.date | None, start: dt.date, end: dt.date) -> bool:
    return (lo is not None and lo <= start + dt.timedelta(days=4)
            and hi >= _last_business_day(end) - dt.timedelta(days=4 if end < dt.date.today() else 1))


def ensure_funds(s: Session, symbols: list[str], start: dt.date, end: dt.date, fetch: Fetcher,
                 now: float | None = None) -> dict[str, str]:
    """Cache adjusted closes for each symbol over [start, end]. A fund whose history starts after `start` is
    fetched once and then only refreshed for new days (at most every REFETCH_HOURS).
    Returns {symbol: error} for the ones without data."""
    errors = {}
    now = time.time() if now is None else now
    # Fetch everything from the network first, then write. Keeping the (slow) downloads out of the
    # write transaction means the SQLite write lock is held only for the fast inserts, so overlapping
    # callbacks/syncs wait briefly instead of piling up on a long-held lock (see PRAGMA busy_timeout).
    fetched: list[tuple[str, list]] = []
    for symbol in symbols:
        lo, hi = s.execute(select(func.min(FundPrice.date), func.max(FundPrice.date))
                           .where(FundPrice.symbol == symbol)).one()
        key = "fund:" + symbol
        recent = key in _last_attempt and now - _last_attempt[key] < REFETCH_HOURS * 3600
        if lo is not None and (_covered(lo, hi, start, end) or recent):
            continue
        if lo is None and recent:
            errors[symbol] = f"No prices for {symbol} (the download failed recently; it is retried in a few hours)"
            continue
        _last_attempt[key] = now
        try:
            rows = fetch(symbol, start, end)
        except BenchmarkError as exc:
            errors[symbol] = str(exc)
            continue
        if not rows:
            errors[symbol] = f"No prices returned for {symbol}"
            continue
        fetched.append((symbol, rows))
    for symbol, rows in fetched:
        for i in range(0, len(rows), 500):  # SQLite caps the number of bound parameters per statement
            chunk = rows[i:i + 500]
            stmt = insert(FundPrice).values([{"symbol": symbol, "date": d, "adj_close": c} for d, c in chunk])
            s.execute(stmt.on_conflict_do_update(index_elements=["symbol", "date"],
                                                 set_={"adj_close": stmt.excluded.adj_close}))
    return errors


def fund_prices(s: Session, symbols: list[str], start: dt.date | None = None) -> pd.DataFrame:
    """Adjusted closes: index = trading date, one column per symbol (NaN before a fund has prices)."""
    if not symbols:
        return pd.DataFrame()
    q = select(FundPrice.symbol, FundPrice.date, FundPrice.adj_close).where(FundPrice.symbol.in_(symbols))
    if start:
        q = q.where(FundPrice.date >= start)
    df = pd.DataFrame(s.execute(q).all(), columns=["symbol", "date", "adj_close"])
    if df.empty:
        return pd.DataFrame(columns=symbols)
    df["date"] = pd.to_datetime(df["date"])
    return df.pivot_table(index="date", columns="symbol", values="adj_close", aggfunc="last").sort_index()


def load_funds(s: Session, symbols: list[str], start: dt.date, end: dt.date,
               demo: bool) -> tuple[pd.DataFrame, dict[str, str]]:
    """(adjusted closes from `start`, {symbol: error}). Writes the cache (caller commits)."""
    errors = ensure_funds(s, symbols, start, end, fake_adjusted if demo else fetch_yahoo_adjusted)
    return fund_prices(s, symbols, start), errors


def _upsert_fund(s: Session, symbol: str, rows: list[tuple[dt.date, float]]) -> None:
    for i in range(0, len(rows), 500):  # SQLite caps the number of bound parameters per statement
        chunk = rows[i:i + 500]
        stmt = insert(FundPrice).values([{"symbol": symbol, "date": d, "adj_close": c} for d, c in chunk])
        s.execute(stmt.on_conflict_do_update(index_elements=["symbol", "date"],
                                             set_={"adj_close": stmt.excluded.adj_close}))


def refresh_fund_prices(s: Session, symbols: list[str], demo: bool, *, full_years: int = 10,
                        recent_days: int = 40, now: float | None = None) -> dict[str, str]:
    """Fill/refresh the cached adjusted closes for `symbols` with as few downloads as possible (caller commits).

    A symbol with no cached rows (brand new ticker, or the first run) is backfilled with `full_years` of
    history; a symbol already cached only has its last `recent_days` refreshed. Each group is ONE batched
    download, so the usual case - all tickers already cached - is a single request. Returns {symbol: error}
    for the ones that returned no data. A new symbol that fails is not retried for REFETCH_HOURS, so a bad
    ticker in the menu does not cause repeated downloads; the daily refresh (>24h apart) still retries it."""
    symbols = sorted({(x or "").upper() for x in symbols if x and x.strip()})
    if not symbols:
        return {}
    fetch_many: ManyFetcher = fake_many_adjusted if demo else fetch_many_adjusted
    today = dt.date.today()
    now = time.time() if now is None else now
    cached = {sym for sym, in s.execute(select(FundPrice.symbol).where(FundPrice.symbol.in_(symbols))
                                        .group_by(FundPrice.symbol)).all()}
    errors: dict[str, str] = {}
    new = []
    for sym in (x for x in symbols if x not in cached):  # backfill, with a per-symbol retry backoff
        key = "fund:" + sym
        if key in _last_attempt and now - _last_attempt[key] < REFETCH_HOURS * 3600:
            errors[sym] = f"No prices for {sym} (the download failed recently; it is retried in a few hours)"
        else:
            _last_attempt[key] = now
            new.append(sym)
    old = [x for x in symbols if x in cached]  # already have history: just top up recent days
    for group, start, kind in ((new, today - dt.timedelta(days=365 * full_years), "backfill (%dy)" % full_years),
                               (old, today - dt.timedelta(days=recent_days), "recent")):
        if not group:
            continue
        try:
            rows_by = fetch_many(group, start, today)
        except BenchmarkError as exc:  # network/parse failure for the whole batch
            log.warning("yfinance download failed for %d ticker(s) [%s]: %s", len(group), kind, exc)
            for sym in group:
                errors[sym] = str(exc)
            continue
        wrote, rows_written = [], 0
        for sym in group:
            rows = rows_by.get(sym) or []
            if rows:
                _upsert_fund(s, sym, rows)
                wrote.append(sym)
                rows_written += len(rows)
            elif sym in new:  # a top-up that returns nothing (e.g. a holiday week) is not an error
                errors[sym] = f"No prices returned for {sym}"
        if wrote:  # DB updated: say what changed
            log.info("Fund prices updated [%s]: %d ticker(s), %d row(s): %s",
                     kind, len(wrote), rows_written, ", ".join(wrote[:15]) + (" ..." if len(wrote) > 15 else ""))
        empty = [x for x in group if not rows_by.get(x)]
        if empty and len(empty) == len(group):  # nothing came back at all -> almost always a rate limit / outage
            log.warning("yfinance returned no data for all %d ticker(s) [%s] - likely rate-limited (HTTP 429) "
                        "or offline; will retry on the next scheduled refresh: %s",
                        len(group), kind, ", ".join(empty[:15]))
        elif empty:
            log.warning("yfinance returned no data for %d of %d ticker(s) [%s]: %s",
                        len(empty), len(group), kind, ", ".join(empty[:15]))
    return errors


def fund_prices_cached(s: Session, symbols: list[str], demo: bool, start: dt.date,
                       now: float | None = None) -> tuple[pd.DataFrame, dict[str, str]]:
    """Adjusted closes from `start`, read from the cache; only symbols with nothing cached (a ticker just
    added to the menu) are fetched, then re-read. The scheduled/startup warm keeps everything else current,
    so opening the optimizer does not download anything. (adjusted closes, {symbol: error})."""
    df = fund_prices(s, symbols, start)
    missing = [x for x in symbols if x not in df.columns or not df[x].notna().any()]
    errors: dict[str, str] = {}
    if missing:
        errors = refresh_fund_prices(s, missing, demo, now=now)
        df = fund_prices(s, symbols, start)
    return df, errors


def closes(s: Session, symbol: str, start: dt.date | None = None, end: dt.date | None = None) -> pd.Series:
    q = select(BenchmarkPrice.date, BenchmarkPrice.close).where(BenchmarkPrice.symbol == symbol)
    if start:
        q = q.where(BenchmarkPrice.date >= start - dt.timedelta(days=7))  # a close on or before `start`
    if end:
        q = q.where(BenchmarkPrice.date <= end)
    rows = s.execute(q.order_by(BenchmarkPrice.date)).all()
    if not rows:
        return pd.Series(dtype=float)
    return pd.Series([c for _, c in rows], index=pd.to_datetime([d for d, _ in rows]), name=symbol)


def load(s: Session, symbol: str, start: dt.date, end: dt.date, demo: bool) -> tuple[pd.Series, str | None]:
    """(closes covering [start, end], error message or None). Writes the cache (caller commits)."""
    error = ensure(s, symbol, start, end, fake_prices if demo else fetch_yahoo)
    return closes(s, symbol, start, end), error


def return_index(prices: pd.Series, index: pd.DatetimeIndex) -> pd.Series | None:
    """Cumulative return over `index` (0 on its first day), last close carried over weekends/holidays."""
    if prices.empty or not len(index):
        return None
    daily = prices.reindex(prices.index.union(index)).sort_index().ffill().reindex(index)
    if daily.isna().all():
        return None
    base = daily.dropna().iloc[0]
    return daily / base - 1
