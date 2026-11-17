"""Markowitz optimizer, fund menus, fund prices and the Optimizer page (demo household 401(k))."""
import base64
import datetime as dt

import numpy as np
import pandas as pd
import pytest

from finance import benchmarks as bm
from finance import db as dbmod
from finance import fundmenu as fm
from finance import optimizer as op
from finance import services
from finance.config import get_settings
from finance.demo import PLAN_MENU, build_demo

TODAY = dt.date(2026, 11, 9)
SYMS = ["VFIAX", "VTIAX", "VBTLX", "VGSLX", "VSMAX"]


def fake_prices(symbols=SYMS, start=dt.date(2012, 1, 2), end=TODAY) -> pd.DataFrame:
    df = pd.DataFrame({s: pd.Series(dict(bm.fake_adjusted(s, start, end))) for s in symbols})
    df.index = pd.to_datetime(df.index)
    return df


@pytest.fixture
def demo(tmp_path, monkeypatch):
    monkeypatch.setenv("FINANCE_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("FINANCE_DEMO", "1")
    info = build_demo(get_settings(), today=TODAY)
    bm._last_attempt.clear()
    yield info
    for eng in dbmod._engines.values():
        eng.dispose()
    dbmod._engines.clear()
    services._initialized.clear()


@pytest.mark.parametrize("text, funds, skipped", [
    ("VFIAX VBTLX, vtiax", [("VFIAX", None), ("VBTLX", None), ("VTIAX", None)], []),
    ("VFIAX, Vanguard 500 Index Admiral\nVBTLX - Total Bond\nStable Value Fund",
     [("VFIAX", "Vanguard 500 Index Admiral"), ("VBTLX", "Total Bond")], ["Stable Value Fund"]),
    ("﻿Ticker,Fund name\nVFIAX,Vanguard 500\n,Stable Value\nvfiax,dupe\n", [("VFIAX", "Vanguard 500")],
     [", Stable Value"]),
    ("Fund Name,Symbol\nBond fund,VBTLX\n", [("VBTLX", "Bond fund")], []),
    ("", [], []),
])
def test_parse_fund_list(text, funds, skipped):
    parsed = op.parse_fund_list(text)
    assert parsed.funds == funds and parsed.skipped == skipped


def test_optimizer_math():
    prices = fake_prices()
    mu, cov = op.moments(prices.iloc[-1260:])
    lr = op.log_returns(prices.iloc[-1260:])
    assert mu == pytest.approx(lr.mean().to_numpy() * 252) and cov == pytest.approx(lr.cov().to_numpy() * 252)

    ms = op.max_sharpe(mu, cov, cap=0.4)
    mv = op.min_variance(mu, cov, cap=0.4)
    for p in (ms, mv):
        assert p.weights.sum() == pytest.approx(1) and p.weights.min() >= 0 and p.weights.max() <= 0.4 + 1e-9
    rng = np.random.default_rng(1)
    random_ports = [op.evaluate(w, mu, cov) for w in rng.dirichlet(np.ones(len(mu)), 5000) if w.max() <= 0.4]
    assert ms.sharpe >= max(p.sharpe for p in random_ports) - 1e-6
    assert mv.vol <= min(p.vol for p in random_ports) + 1e-6

    front = op.frontier(mu, cov, cap=0.4, points=12)
    rets, vols = [p.ret for p in front], [p.vol for p in front]
    assert rets == sorted(rets) and vols[0] == pytest.approx(mv.vol, rel=1e-3)
    assert all(b >= a - 1e-6 for a, b in zip(vols, vols[1:]))  # more return costs more risk
    assert rets[-1] == pytest.approx(op.max_return(mu, 0.4), abs=1e-4)
    mid = op.frontier_point(mu, cov, (rets[0] + rets[-1]) / 2, cap=0.4)
    assert mid.ret == pytest.approx((rets[0] + rets[-1]) / 2, abs=1e-5)
    assert op.frontier_point(mu, cov, 99.0, cap=0.4).ret == pytest.approx(rets[-1], abs=1e-4)  # clipped

    assert op.effective_cap(0.1, 5) == 0.2  # 5 funds cannot each stay under 10%
    with_rf = op.max_sharpe(mu, cov, rf=0.03, cap=0.4)
    assert with_rf.sharpe == pytest.approx((with_rf.ret - 0.03) / with_rf.vol)

    r = op.mix_returns(op.simple_returns(prices.iloc[-300:]), ms.weights)
    perf = op.performance(r)
    assert perf["max_drawdown"] <= 0 and perf["days"] == 299
    assert op.cumulative(r).iloc[-1] == pytest.approx((1 + r).prod() - 1)


def test_walk_forward_never_uses_future_prices():
    prices = fake_prices(end=dt.date(2020, 12, 31))
    wf = op.walk_forward(prices, train_years=3, rf=0.0, cap=0.5, fixed={"Equal weight": np.full(5, 0.2)})
    assert wf is not None and len(wf.periods) == 6 and set(wf.returns) == {"Max Sharpe", "Min variance",
                                                                          "Equal weight"}
    assert wf.periods[0]["start"] >= dt.date(2015, 1, 2)
    # changing the last year's prices must not change any earlier decision
    shocked = prices.copy()
    shocked.loc[shocked.index >= "2020-01-01", "VFIAX"] *= 3
    wf2 = op.walk_forward(shocked, train_years=3, rf=0.0, cap=0.5)
    for p1, p2 in zip(wf.periods[:-1], wf2.periods[:-1]):
        assert p1["weights"]["Max Sharpe"] == pytest.approx(p2["weights"]["Max Sharpe"], abs=1e-9)
    assert op.walk_forward(prices.iloc[-400:], train_years=3) is None  # too short


def test_analyze_window_limiter_and_current():
    prices = fake_prices(SYMS + ["VTAPX"])
    prices.loc[prices.index < "2023-06-01", "VTAPX"] = np.nan  # a young fund
    a = op.analyze(prices, lookback_years=5, rf=0.0, cap=0.4, train_years=3, current={"VFIAX": 0.6, "VBTLX": 0.4})
    assert a.limiter == "VTAPX" and a.start >= pd.Timestamp("2023-06-01")
    assert a.current.weights == pytest.approx([0.6, 0, 0.4, 0, 0, 0])
    assert a.max_sharpe.sharpe >= a.current.sharpe
    assert set(a.walk.returns) == {"Max Sharpe", "Min variance", "Current mix", "Equal weight"}
    short = op.analyze(prices, lookback_years=1, rf=0.0, cap=0.4, train_years=3)
    assert short.limiter is None and short.current is None  # the 1-year window is not cut short
    assert op.analyze(prices[["VFIAX"]], 5, 0.0, 0.4, 3) is None  # needs two funds


def test_fund_menu_and_current_mix(demo):
    menu = services.read(fm.menu, "DEMO-401K")
    assert [m["symbol"] for m in menu] == sorted(s for s, _ in PLAN_MENU[:-1])
    added, existing = services.write(fm.add_funds, "DEMO-401K", [("VTAPX", "TIPS"), ("VFIAX", "x")])
    assert (added, existing) == (1, 1)
    assert {m["symbol"]: m["name"] for m in services.read(fm.menu, "DEMO-401K")}["VFIAX"].startswith("Vanguard 500")
    mix, outside = services.read(fm.current_mix, "DEMO-401K", ["VFIFX", "VFIAX"])
    assert mix == {"VFIFX": pytest.approx(1.0)} and outside == []
    mix, outside = services.read(fm.current_mix, "DEMO-BRK", ["VTI", "BND"])
    assert sum(mix.values()) == pytest.approx(1) and {o[0] for o in outside} == {"VXUS", "SPAXX"}
    vtapx = next(m for m in services.read(fm.menu, "DEMO-401K") if m["symbol"] == "VTAPX")
    services.write(fm.rename, vtapx["id"], "  TIPS fund ")
    assert services.write(fm.remove, [vtapx["id"]]) == 1
    assert services.write(fm.add_current_holdings, "DEMO-BRK") == (4, 0)
    with pytest.raises(fm.FundMenuError):
        services.write(fm.add_funds, "", [("VTI", None)])


def test_fund_price_cache_and_errors(demo):
    calls = []

    def fetch(symbol, start, end):
        calls.append(symbol)
        if symbol == "BADX":
            raise bm.BenchmarkError("BADX is not a symbol Yahoo Finance knows")
        return bm.fake_adjusted(symbol, start, end)

    lo = dt.date(2015, 1, 1)
    errors = services.write(bm.ensure_funds, ["VFIAX", "BADX"], lo, TODAY, fetch, now=1000.0)
    assert set(errors) == {"BADX"} and calls == ["VFIAX", "BADX"]
    errors = services.write(bm.ensure_funds, ["VFIAX", "BADX"], lo, TODAY, fetch, now=1001.0)
    assert calls == ["VFIAX", "BADX"] and "retried" in errors["BADX"]  # cached / not hammered
    prices = services.read(bm.fund_prices, ["VFIAX", "BADX"], lo)
    assert list(prices.columns) == ["VFIAX"] and prices.index.min() >= pd.Timestamp(lo)
    assert len(prices) > 2500  # > 500 rows: inserted in chunks


def test_page_callbacks(demo, monkeypatch):
    from finance.app.main import create_app
    from finance.app.pages import optimizer as page

    create_app()
    page._analysis.cache_clear()
    assert page.layout()
    rows = page.menu_rows("DEMO-401K", 0)
    assert {r["symbol"]: r["current"] for r in rows}["VFIFX"] == pytest.approx(1.0)

    out = page.run_analysis("DEMO-401K", "5", 40, 0, "3", 0, 0, "light")
    msgs, lo, hi, value, marks, walk, walk_rows, walk_sub = out
    assert lo < value <= hi and "Min variance" in str(marks) and walk.data
    assert {r["strategy"].split(" (")[0] for r in walk_rows} == {"Max Sharpe", "Min variance", "Current mix",
                                                                "Equal weight"}
    assert "never uses future prices" in walk_sub
    for theme in ("light", "dark"):
        fig, label, stats, _, weights, wchart, backtest, _ = page.update_selected(
            value, "DEMO-401K", "5", 40, 0, "3", 0, 0, theme)
        assert {t.name for t in fig.data} >= {"Funds", "Efficient frontier", "Current mix", "Max Sharpe",
                                               "Min variance", "Selected"}
        assert [s["portfolio"] for s in stats] == ["Current mix", "Max Sharpe", "Min variance", "Selected",
                                                   "Equal weight"]
        assert sum(w["max_sharpe"] for w in weights) == pytest.approx(1) and max(w["max_sharpe"] for w in weights) <= 0.4 + 1e-6
        assert len(wchart.data) == 2 and len(backtest.data) == 5 and "Selected: expected return" in label
    low = page.update_selected(lo, "DEMO-401K", "5", 40, 0, "3", 0, 0, "light")[2]
    assert low[3]["vol"] == pytest.approx(low[2]["vol"], rel=1e-3)  # slider at the left end = min variance

    # Performance vs market: portfolio + benchmark with no ticker, + the clicked ticker when one is given.
    fig, sub = page.ticker_performance(None, "SPY", "DEMO-401K", "5", 40, 0, "3", 0, 0, "light")
    names = [t.name for t in fig.data]
    assert "Current mix" in names and "S&P 500 (SPY)" in names and len(fig.data) == 2
    assert "Current mix" in sub and "Click a ticker" in sub
    fig2, sub2 = page.ticker_performance("VFIAX", "QQQ", "DEMO-401K", "5", 40, 0, "3", 0, 0, "light")
    names2 = [t.name for t in fig2.data]
    assert names2 == ["Current mix", "VFIAX", "Nasdaq-100 (QQQ)"] and "VFIAX" in sub2

    # upload the demo CSV: adds the one fund not pre-loaded and skips the fund without a ticker
    payload = "data:text/csv;base64," + base64.b64encode(demo["fund_menu_path"].read_bytes()).decode()
    monkeypatch.setattr(page, "ctx", type("Ctx", (), {"triggered_id": "opt-upload"})())
    msg, refresh, _ = page.edit_menu(payload, None, None, None, None, "fund_menu_401k.csv", None, "DEMO-401K", None, 0)
    assert refresh == 1 and "Added 1 fund" in str(msg) and "Stable Value" in str(msg)
    monkeypatch.setattr(page, "ctx", type("Ctx", (), {"triggered_id": "opt-add"})())
    msg, refresh, cleared = page.edit_menu(None, 1, None, None, None, None, "Stable Value Fund", "DEMO-401K", None, 1)
    assert "No tickers" in str(msg) and refresh is page.no_update
    msg, refresh, cleared = page.edit_menu(None, 2, None, None, None, None, "VTI", "DEMO-401K", None, 1)
    assert refresh == 2 and cleared == ""
    monkeypatch.setattr(page, "ctx", type("Ctx", (), {"triggered_id": "opt-remove"})())
    rows = page.menu_rows("DEMO-401K", 2)
    vti = [r for r in rows if r["symbol"] == "VTI"]
    msg, refresh, _ = page.edit_menu(None, None, None, 1, None, None, None, "DEMO-401K", vti, 2)
    assert "Removed 1" in str(msg) and refresh == 3

    # an account with fewer than two funds explains what to do
    msgs = page.run_analysis("DEMO-BRK", "5", 40, 0, "3", 0, 0, "light")[0]
    assert "at least two funds" in str(msgs)
