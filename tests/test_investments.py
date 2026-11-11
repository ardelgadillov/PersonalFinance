"""Investment analytics and page callbacks on the demo household (brokerage + 401(k), a year of snapshots)."""
import datetime as dt

import pandas as pd
import pytest
from sqlalchemy import delete, update

from finance import db as dbmod
from finance import investments as inv
from finance import networth as nw
from finance import services
from finance.config import get_settings
from finance.demo import build_demo
from finance.models import HoldingSnapshot

TODAY = dt.date(2026, 11, 9)
START = dt.date(2026, 1, 1)


@pytest.fixture
def demo(tmp_path, monkeypatch):
    monkeypatch.setenv("FINANCE_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("FINANCE_DEMO", "1")
    build_demo(get_settings(), today=TODAY)
    yield
    for eng in dbmod._engines.values():
        eng.dispose()
    dbmod._engines.clear()
    services._initialized.clear()


def test_accounts_and_stable_slots(demo):
    accts = services.read(inv.investment_accounts)
    assert {a["id"] for a in accts} == {"DEMO-BRK", "DEMO-401K"}
    slots = {a["id"]: a["slot"] for a in accts}
    only = services.read(inv.investment_accounts, ["DEMO-401K"])
    assert [a["slot"] for a in only] == [slots["DEMO-401K"]]  # a filter never repaints an account
    services.update_account("DEMO-BRK", "hidden", True)
    assert [a["id"] for a in services.read(inv.investment_accounts)] == ["DEMO-401K"]


def test_holdings_gain_weights_and_price_change(demo):
    accts, daily, h = services.read(inv.load)
    rows = inv.holding_rows(h, accts, START, TODAY)
    assert {r["symbol"] for r in rows} == {"VTI", "VXUS", "BND", "SPAXX", "VFIFX"}
    assert sum(r["weight"] for r in rows) == pytest.approx(1)
    assert rows == sorted(rows, key=lambda r: -r["market_value"])
    for r in rows:
        assert r["gain"] == pytest.approx(r["market_value"] - r["cost_basis"], abs=0.01)
        assert r["price"] == pytest.approx(r["market_value"] / r["shares"], rel=1e-3)
    spaxx = next(r for r in rows if r["symbol"] == "SPAXX")
    assert spaxx["gain"] == 0 and spaxx["price_change"] == pytest.approx(0)  # money market: price 1.00

    # holdings total matches the account balances on the same day (demo balance = holdings value)
    values = inv.value_by_account(nw.clip(daily, START, TODAY), accts)
    k = inv.kpis(values, rows)
    assert k["value"] == pytest.approx(k["holdings_value"], abs=0.05)
    assert k["gain"] == pytest.approx(sum(r["gain"] for r in rows), abs=0.05)
    assert k["change"] == pytest.approx(values["total"].iloc[-1] - values["total"].iloc[0], abs=0.01)

    past = inv.holding_rows(h, accts, None, dt.date(2026, 3, 31))
    assert {r["as_of"] for r in past} == {"2026-03-31"}


def test_allocation_cost_and_history(demo):
    accts, daily, h = services.read(inv.load)
    rows = inv.holding_rows(h, accts, START, TODAY)
    by_sym = inv.allocation(rows, "symbol")
    assert by_sym["share"].sum() == pytest.approx(1) and by_sym["value"].is_monotonic_decreasing
    by_acct = inv.allocation(rows, "account")
    assert len(by_acct) == 2
    folded = inv.allocation(rows, "symbol", max_items=3)
    assert len(folded) == 3 and folded["label"].iloc[-1] == "Other (3)"
    assert folded["value"].sum() == pytest.approx(by_sym["value"].sum())

    idx = nw.clip(daily, START, TODAY).index
    cost = inv.cost_daily(h, idx)
    assert len(cost) == len(idx) and cost.iloc[-1] == pytest.approx(sum(r["cost_basis"] for r in rows), abs=0.05)

    hist = inv.holding_history(h, "DEMO-BRK", "DEMO-BRK-VTI")
    assert hist["date"].is_monotonic_increasing and hist["price"].notna().all()


def test_missing_cost_basis_and_empty(demo):
    services.write(lambda s: s.execute(update(HoldingSnapshot).values(cost_basis=None)))
    accts, daily, h = services.read(inv.load)
    rows = inv.holding_rows(h, accts, START, TODAY)
    assert all(r["gain"] is None and r["gain_pct"] is None for r in rows)
    assert inv.kpis(inv.value_by_account(daily, accts), rows)["gain"] is None
    assert inv.cost_daily(h, daily.index) is None

    services.write(lambda s: s.execute(delete(HoldingSnapshot)))
    _, _, h = services.read(inv.load)
    assert inv.holding_rows(h, accts, START, TODAY) == [] and inv.allocation([], "symbol").empty


def test_focus_values_and_returns(demo):
    accts, daily, h = services.read(inv.load)
    idx = nw.clip(daily, START, TODAY).index
    vti = inv.focus_holdings(h, "VTI")
    assert set(vti["symbol"]) == {"VTI"} and inv.focus_holdings(h, None) is h
    values = inv.holding_values(vti, idx, accts)
    rows = inv.holding_rows(vti, accts, START, TODAY)
    assert values["total"].iloc[-1] == pytest.approx(rows[0]["market_value"], abs=0.01)

    # one holding: the chained return is its price change; the window starts at 0
    r = inv.return_index(vti, idx)
    assert r.iloc[0] == 0 and r.iloc[-1] == pytest.approx(rows[0]["price_change"], abs=1e-6)
    # portfolio: buys and deposits do not count (money market at $1.00 never moves)
    assert inv.return_index(inv.focus_holdings(h, "SPAXX"), idx).abs().max() < 1e-9
    port = inv.return_index(h, idx)
    changes = [x["price_change"] for x in inv.holding_rows(h, accts, START, TODAY)]
    assert min(changes) - 1e-9 <= port.iloc[-1] <= max(changes) + 1e-9
    no_shares = h.assign(shares=None)
    assert inv.return_index(no_shares, idx) is None

    alloc = inv.allocation(inv.holding_rows(h, accts, START, TODAY), "account")
    assert set(alloc["key"]) == {"DEMO-BRK", "DEMO-401K"}


def test_benchmark_cache_and_errors(demo, monkeypatch):
    from finance import benchmarks as bm

    calls = []

    def fetch(symbol, start, end):
        calls.append((symbol, start, end))
        return bm.fake_prices(symbol, start, end)

    lo, hi = dt.date(2026, 1, 1), dt.date(2026, 6, 30)
    bm._last_attempt.clear()
    assert services.write(bm.ensure, "SPY", lo, hi, fetch, now=1000.0) is None
    assert services.write(bm.ensure, "SPY", lo, hi, fetch, now=1001.0) is None
    assert len(calls) == 1  # cached
    closes = services.read(bm.closes, "SPY", lo, hi)
    assert closes.index.min() <= pd.Timestamp(lo) and closes.index.max() >= pd.Timestamp("2026-06-26")
    idx = pd.date_range(lo, hi, freq="D")
    r = bm.return_index(closes, idx)
    assert r.iloc[0] == 0 and r.notna().all()  # weekends carry the last close
    assert bm.fake_prices("SPY", lo, hi) == bm.fake_prices("SPY", lo, hi)  # deterministic

    def broken(symbol, start, end):
        raise bm.BenchmarkError("offline")

    assert services.write(bm.ensure, "QQQ", lo, hi, broken, now=2000.0) == "offline"
    assert services.write(bm.ensure, "QQQ", lo, hi, broken, now=2000.0 + 3600) == "offline"  # nothing cached
    # a longer range than cached is refetched, but at most every REFETCH_HOURS
    assert services.write(bm.ensure, "SPY", dt.date(2025, 6, 1), hi, broken, now=1002.0) is None
    assert services.write(bm.ensure, "SPY", dt.date(2025, 6, 1), hi, broken,
                          now=1002.0 + bm.REFETCH_HOURS * 3600) == "offline"
    monkeypatch.setattr(bm, "_session", lambda: (_ for _ in ()).throw(AssertionError("no network in demo")))
    prices, err = services.write(bm.load, "VT", lo, hi, True)
    assert err is None and not prices.empty


def test_page_callbacks(demo, monkeypatch):
    from finance.app.main import create_app
    from finance.app.pages import investments as page

    create_app()
    assert page.layout()
    for theme in ("light", "dark"):
        kpis, hist, title, subtitle, alloc, rows, dates_style, chip, _ = page.update(
            "12m", None, None, None, "symbol", None, theme, 0)
        assert len(kpis) == 4 and dates_style == {"display": "none"} and chip == {"display": "none"} and rows
        # glow_lines adds showlegend=False underlays beneath lines; count the visible traces only
        names = [t.name for t in hist.data if t.showlegend is not False]
        assert names[-1] == "Cost basis" and len(names) == 3 and alloc.data and title.startswith("Portfolio")
        assert alloc.layout.height == page.ALLOC_HEIGHT
        fig, title, sub = page.update_holding([rows[1]], rows, "12m", None, None, theme, 0)
        visible = [t for t in fig.data if t.showlegend is not False]
        assert len(visible) == 2 and title.startswith(rows[1]["symbol"]) and "click another" not in sub
        assert "largest" in page.update_holding(None, rows, "12m", None, None, theme, 0)[2]
        perf, stats = page.update_performance([rows[1]], rows, None, "SPY", "12m", None, None, None, theme, 0)
        perf_names = [t.name for t in perf.data if t.showlegend is not False]
        assert perf_names == ["Portfolio", rows[1]["symbol"], "S&P 500 (SPY)"] and len(stats) == 4
    assert page.update_holding(None, [], "12m", None, None, "light", 0)[1] == "Holding history"

    # the allocation chart keeps its size whatever the number of bars
    by_acct = page.update("12m", None, None, None, "account", None, "light", 0)[4]
    acct_bar = next(t for t in by_acct.data if t.showlegend is not False)  # skip glow underlays
    assert by_acct.layout.height == page.ALLOC_HEIGHT and len(acct_bar.y) == 2

    # clicking a holding bar filters the page to it; clicking it again clears; an account bar sets the filter
    click = {"points": [{"customdata": [0.2, "VTI"], "y": "VTI"}]}
    monkeypatch.setattr(page, "ctx", type("Ctx", (), {"triggered_id": "inv-allocation"})())
    assert page.click_allocation(click, None, "symbol", None) == ("VTI", page.no_update)
    assert page.click_allocation(click, None, "symbol", "VTI") == (None, page.no_update)
    assert page.click_allocation({"points": [{"customdata": [0.6, "DEMO-401K"]}]}, None, "account", "VTI") == (
        None, ["DEMO-401K"])
    assert page.click_allocation({"points": [{"customdata": [0.1, ""]}]}, None, "symbol", None) == (
        page.no_update, page.no_update)
    monkeypatch.setattr(page, "ctx", type("Ctx", (), {"triggered_id": "inv-focus-clear"})())
    assert page.click_allocation(click, 1, "symbol", "VTI") == (None, page.no_update)

    kpis, hist, title, _, alloc, rows, _, chip, label = page.update("12m", None, None, None, "symbol", "VTI",
                                                                    "light", 0)
    assert title == "VTI value over time" and chip == {} and label == "Filtered to VTI"
    assert [r["symbol"] for r in rows] == ["VTI"] and "VTI" in kpis[0].children[0].children
    alloc_bar = next(t for t in alloc.data if t.showlegend is not False)  # skip glow underlays
    assert len(set(alloc_bar.marker.color)) == 2  # the focused bar stands out
    perf, _ = page.update_performance(None, rows, "VTI", "SPY", "12m", None, None, None, "light", 0)
    assert [t.name for t in perf.data if t.showlegend is not False] == ["Portfolio", "VTI", "S&P 500 (SPY)"]

    custom = page.update("custom", "2026-03-01", "2026-03-31", ["DEMO-401K"], "account", None, "light", 0)
    assert custom[6] == {} and [r["symbol"] for r in custom[5]] == ["VFIFX"]
    assert pd.notna(custom[5][0]["price_change"])
