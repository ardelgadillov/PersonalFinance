"""Markowitz portfolio optimization for an account's fund menu (numpy / scipy, no UI).

Follows the QuiraData notebook (docs/optimization/portfolio_optimization.ipynb):
- daily log returns of dividend-adjusted closes; expected return = mean * 252, covariance = cov * 252
- volatility = sqrt(w' Cov w); Sharpe = (return - risk-free) / volatility (risk-free 0 in the notebook)
- scipy.optimize.minimize (SLSQP) from equal weights, weights sum to 1, long only (bounds 0..cap)
- efficient frontier = minimum volatility for each target return
- historical performance = weights applied to daily simple returns (a constant mix, rebalanced daily)
Added here: a cap per fund, a minimum-variance portfolio, a frontier point picked by target return, and a
walk-forward (out-of-sample) test that re-optimizes every year on the trailing training window only.
"""
from __future__ import annotations

import csv
import io
import re
from dataclasses import dataclass, field

import numpy as np
import pandas as pd
from scipy.optimize import minimize

TRADING_DAYS = 252
SYMBOL_RE = re.compile(r"^[A-Z0-9][A-Z0-9.\-^]{0,11}$")
SYMBOL_HEADERS = {"symbol", "ticker", "fund symbol", "ticker symbol", "fund ticker", "code"}
NAME_HEADERS = {"name", "fund", "fund name", "description", "investment", "investment name"}


# --------------------------------------------------------------------------- fund list parsing
@dataclass
class ParsedFunds:
    funds: list[tuple[str, str | None]] = field(default_factory=list)  # (symbol, name)
    skipped: list[str] = field(default_factory=list)  # rows without a usable ticker


def parse_fund_list(text: str) -> ParsedFunds:
    """A CSV with a ticker/symbol column (and optional name column), or plain text with one fund per line
    ("VFIAX" or "VFIAX, Vanguard 500 Index Admiral"), or tickers separated by commas/spaces."""
    out = ParsedFunds()
    seen: set[str] = set()
    text = (text or "").lstrip("﻿").strip()
    if not text:
        return out

    def add(symbol: str, name: str | None, raw: str) -> None:
        sym = (symbol or "").strip().upper()
        if not SYMBOL_RE.match(sym):
            if raw.strip():
                out.skipped.append(raw.strip())
            return
        if sym not in seen:
            seen.add(sym)
            out.funds.append((sym, (name or "").strip() or None))

    rows = list(csv.reader(io.StringIO(text)))
    header = [h.strip().lower() for h in rows[0]] if rows else []
    sym_col = next((i for i, h in enumerate(header) if h in SYMBOL_HEADERS), None)
    if sym_col is not None:
        name_col = next((i for i, h in enumerate(header) if h in NAME_HEADERS), None)
        for r in rows[1:]:
            if not any(c.strip() for c in r):
                continue
            sym = r[sym_col] if sym_col < len(r) else ""
            name = r[name_col] if name_col is not None and name_col < len(r) else None
            add(sym, name, ", ".join(r))
        return out
    for ln in (ln for ln in text.splitlines() if ln.strip()):
        tokens = [t for t in re.split(r"[\s,;\t]+", ln.strip()) if t]
        if all(_looks_like_ticker(t) for t in tokens):  # "VFIAX VBTLX, VTIAX"
            for t in tokens:
                add(t, None, t)
        elif _looks_like_ticker(tokens[0]):  # "VFIAX, Vanguard 500 Index Admiral" / "VFIAX - Vanguard 500"
            add(tokens[0], re.sub(r"^[\s,;\t\-:]+", "", ln.strip()[len(tokens[0]):]), ln)
        else:
            out.skipped.append(ln.strip())
    return out


def _looks_like_ticker(token: str) -> bool:
    """VFIAX / vfiax / BRK.B, but not words of a fund name (Index, 500, Admiral)."""
    return (bool(SYMBOL_RE.match(token.upper())) and len(token) <= 8 and any(ch.isalpha() for ch in token)
            and token in (token.upper(), token.lower()))


# --------------------------------------------------------------------------- statistics
def common_history(prices: pd.DataFrame) -> tuple[pd.DataFrame, str | None]:
    """Rows where every fund has a price (forward-filled over holidays of single funds).
    Returns (prices, the symbol that limits the start, if any)."""
    if prices.empty:
        return prices, None
    filled = prices.ffill()
    starts = filled.apply(lambda c: c.first_valid_index())
    limiter = starts.idxmax() if starts.notna().all() else None
    return filled.dropna(), (limiter if limiter is not None and starts[limiter] > prices.index[0] else None)


def log_returns(prices: pd.DataFrame) -> pd.DataFrame:
    return np.log(prices / prices.shift(1)).dropna()


def simple_returns(prices: pd.DataFrame) -> pd.DataFrame:
    return prices.pct_change(1).dropna()


def moments(prices: pd.DataFrame) -> tuple[np.ndarray, np.ndarray]:
    """Annualized expected log returns and covariance (notebook: mean * 252, cov * 252)."""
    lr = log_returns(prices)
    return lr.mean().to_numpy() * TRADING_DAYS, lr.cov().to_numpy() * TRADING_DAYS


@dataclass
class Portfolio:
    weights: np.ndarray
    ret: float
    vol: float
    sharpe: float | None


def evaluate(w: np.ndarray, mu: np.ndarray, cov: np.ndarray, rf: float = 0.0) -> Portfolio:
    w = np.asarray(w, dtype=float)
    ret = float(w @ mu)
    vol = float(np.sqrt(max(w @ cov @ w, 0.0)))
    return Portfolio(w, ret, vol, (ret - rf) / vol if vol > 0 else None)


def effective_cap(cap: float, n: int) -> float:
    """A cap below 1/n is infeasible (weights must sum to 1); raise it to 1/n."""
    return float(min(1.0, max(cap, 1.0 / n)))


def _solve(objective, n: int, cap: float, extra: tuple = ()) -> np.ndarray:
    cons = ({"type": "eq", "fun": lambda w: np.sum(w) - 1},) + extra
    res = minimize(objective, x0=np.full(n, 1.0 / n), method="SLSQP", bounds=[(0.0, cap)] * n,
                   constraints=cons, options={"maxiter": 500, "ftol": 1e-10})
    w = np.clip(res.x, 0.0, cap)
    return w / w.sum()


def max_sharpe(mu: np.ndarray, cov: np.ndarray, rf: float = 0.0, cap: float = 1.0) -> Portfolio:
    n = len(mu)
    cap = effective_cap(cap, n)

    def neg_sharpe(w):
        vol = np.sqrt(max(w @ cov @ w, 1e-16))
        return -(w @ mu - rf) / vol

    return evaluate(_solve(neg_sharpe, n, cap), mu, cov, rf)


def min_variance(mu: np.ndarray, cov: np.ndarray, rf: float = 0.0, cap: float = 1.0) -> Portfolio:
    n = len(mu)
    cap = effective_cap(cap, n)
    return evaluate(_solve(lambda w: w @ cov @ w, n, cap), mu, cov, rf)


def max_return(mu: np.ndarray, cap: float = 1.0) -> float:
    """Highest expected return reachable under the cap: fill the best funds up to the cap."""
    cap = effective_cap(cap, len(mu))
    left, total = 1.0, 0.0
    for r in sorted(mu, reverse=True):
        take = min(cap, left)
        total += take * r
        left -= take
        if left <= 1e-12:
            break
    return float(total)


def frontier_point(mu: np.ndarray, cov: np.ndarray, target: float, rf: float = 0.0,
                   cap: float = 1.0) -> Portfolio:
    """Minimum volatility portfolio with expected return = target (clipped to the reachable range)."""
    n = len(mu)
    cap = effective_cap(cap, n)
    lo = min_variance(mu, cov, rf, cap).ret
    target = float(np.clip(target, lo, max_return(mu, cap)))
    cons = ({"type": "eq", "fun": lambda w: w @ mu - target},)
    return evaluate(_solve(lambda w: np.sqrt(max(w @ cov @ w, 1e-16)), n, cap, cons), mu, cov, rf)


def frontier(mu: np.ndarray, cov: np.ndarray, rf: float = 0.0, cap: float = 1.0,
             points: int = 30) -> list[Portfolio]:
    """The efficient part of the frontier: from the minimum-variance return to the highest reachable return."""
    lo, hi = min_variance(mu, cov, rf, cap).ret, max_return(mu, cap)
    if hi - lo < 1e-9:
        return [min_variance(mu, cov, rf, cap)]
    return [frontier_point(mu, cov, t, rf, cap) for t in np.linspace(lo, hi, points)]


# --------------------------------------------------------------------------- backtests
def mix_returns(rets: pd.DataFrame, w: np.ndarray) -> pd.Series:
    """Daily returns of a constant mix (notebook: np.dot(weights, daily_returns.T))."""
    return pd.Series(rets.to_numpy() @ np.asarray(w, dtype=float), index=rets.index)


def cumulative(daily: pd.Series) -> pd.Series:
    return (1 + daily).cumprod() - 1


def performance(daily: pd.Series, rf: float = 0.0) -> dict:
    """Realized: CAGR, annualized volatility, Sharpe ((CAGR - rf) / vol), max drawdown."""
    if daily is None or daily.empty:
        return {"cagr": None, "vol": None, "sharpe": None, "max_drawdown": None, "days": 0}
    growth = (1 + daily).cumprod()
    years = len(daily) / TRADING_DAYS
    cagr = float(growth.iloc[-1] ** (1 / years) - 1) if years > 0 else None
    vol = float(daily.std() * np.sqrt(TRADING_DAYS))
    dd = float((growth / growth.cummax() - 1).min())
    return {"cagr": cagr, "vol": vol, "sharpe": (cagr - rf) / vol if cagr is not None and vol > 0 else None,
            "max_drawdown": dd, "days": len(daily)}


@dataclass
class WalkForward:
    returns: dict[str, pd.Series]  # strategy -> out-of-sample daily returns
    periods: list[dict]  # [{start, end, weights: {strategy: array}}]


def walk_forward(prices: pd.DataFrame, train_years: int = 3, step_months: int = 12, rf: float = 0.0,
                 cap: float = 1.0, fixed: dict[str, np.ndarray] | None = None) -> WalkForward | None:
    """Every `step_months`, optimize on the trailing `train_years` and hold the weights until the next step.
    `fixed` mixes (e.g. the current one) are held unchanged over the same test days. None when the history
    is shorter than train_years + 3 months."""
    if prices.empty:
        return None
    first, last = prices.index[0], prices.index[-1]
    test_start = first + pd.DateOffset(years=train_years)
    if test_start + pd.DateOffset(months=3) > last:
        return None
    rets = simple_returns(prices)
    out: dict[str, list[pd.Series]] = {"Max Sharpe": [], "Min variance": []}
    for name in (fixed or {}):
        out[name] = []
    periods = []
    start = test_start
    while start < last:
        end = min(start + pd.DateOffset(months=step_months), last + pd.Timedelta(days=1))
        train = prices[(prices.index >= start - pd.DateOffset(years=train_years)) & (prices.index < start)]
        test = rets[(rets.index >= start) & (rets.index < end)]
        if len(train) > 60 and not test.empty:
            mu, cov = moments(train)
            ws = {"Max Sharpe": max_sharpe(mu, cov, rf, cap).weights,
                  "Min variance": min_variance(mu, cov, rf, cap).weights, **(fixed or {})}
            for name, w in ws.items():
                out[name].append(mix_returns(test, w))
            periods.append({"start": test.index[0].date(), "end": test.index[-1].date(), "weights": ws})
        start = end
    if not periods:
        return None
    return WalkForward({k: pd.concat(v) for k, v in out.items() if v}, periods)


# --------------------------------------------------------------------------- full analysis
@dataclass
class Analysis:
    symbols: list[str]
    start: pd.Timestamp
    end: pd.Timestamp
    limiter: str | None  # fund whose history limits the in-sample window
    mu: np.ndarray
    cov: np.ndarray
    rf: float
    cap: float
    max_sharpe: Portfolio
    min_variance: Portfolio
    frontier: list[Portfolio]
    current: Portfolio | None
    in_sample_returns: pd.DataFrame  # daily simple returns over the window
    walk: WalkForward | None


def analyze(prices: pd.DataFrame, lookback_years: int, rf: float, cap: float, train_years: int,
            current: dict[str, float] | None = None) -> Analysis | None:
    """prices: adjusted closes (all history available). In-sample = last `lookback_years` of the common
    history; walk-forward uses all of the common history. current: {symbol: weight} of the account now."""
    hist, limiter = common_history(prices.dropna(axis=1, how="all"))
    if hist.shape[1] < 2 or len(hist) < 60:
        return None
    window_start = hist.index[-1] - pd.DateOffset(years=lookback_years)
    window = hist[hist.index >= window_start]
    if limiter is not None and hist.index[0] <= window_start:
        limiter = None  # the window is not cut short
    symbols = list(window.columns)
    mu, cov = moments(window)
    cap = effective_cap(cap, len(symbols))
    cur = None
    fixed = {"Equal weight": np.full(len(symbols), 1.0 / len(symbols))}
    if current:
        w = np.array([current.get(s, 0.0) for s in symbols])
        if w.sum() > 0:
            cur = evaluate(w / w.sum(), mu, cov, rf)
            fixed = {"Current mix": cur.weights, **fixed}
    return Analysis(
        symbols=symbols, start=window.index[0], end=window.index[-1], limiter=limiter, mu=mu, cov=cov, rf=rf,
        cap=cap, max_sharpe=max_sharpe(mu, cov, rf, cap), min_variance=min_variance(mu, cov, rf, cap),
        frontier=frontier(mu, cov, rf, cap), current=cur, in_sample_returns=simple_returns(window),
        walk=walk_forward(hist, train_years, 12, rf, cap, fixed),
    )
