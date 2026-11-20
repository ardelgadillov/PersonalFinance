"""Net worth analytics and page callbacks on the demo household (a year of daily snapshots)."""
import datetime as dt

import pandas as pd
import pytest
from sqlalchemy import delete, select

from finance import db as dbmod
from finance import networth as nw
from finance import services
from finance.config import get_settings
from finance.demo import build_demo
from finance.models import Account, BalanceSnapshot

TODAY = dt.date(2026, 11, 9)


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


def test_signs_groups_and_totals(demo):
    daily, accts = services.read(nw.load)
    groups_of = dict(zip(accts["id"], accts["group"]))
    assert groups_of["DEMO-CHK"] == "cash" and groups_of["DEMO-BRK"] == "investments"
    assert groups_of["DEMO-CC"] == "credit" and groups_of["DEMO-AUTO"] == "loans"
    assert "DEMO-OLDCHK" not in groups_of  # Empower history only: no snapshots
    assert (daily["DEMO-AUTO"] <= 0).all() and (daily["DEMO-BRK"] >= 0).all()
    assert daily.index.is_monotonic_increasing and daily.index.freq == "D"

    g = nw.by_group(daily, accts)
    assert g["net_worth"].iloc[-1] == pytest.approx(daily.iloc[-1].sum())
    assert (g["liabilities"] <= 0).all() and (g["assets"] >= 0).all()
    k = nw.kpis(g)
    assert k["net_worth"] == pytest.approx(k["assets"] + k["liabilities"], abs=0.01)
    assert k["end"] == TODAY and k["change"] == pytest.approx(g["net_worth"].iloc[-1] - g["net_worth"].iloc[0], abs=0.01)

    # a bank reporting a loan as a positive balance still counts as debt
    assert nw.signed("mortgage", 250_000) == -250_000 and nw.signed("savings", -5) == -5
    assert nw.account_group(None, -10) == "loans" and nw.account_group(None, 10) == "other"


def test_monthly_change_and_rows(demo):
    daily, accts = services.read(nw.load)
    clipped = nw.clip(daily, dt.date(2026, 1, 1), dt.date(2026, 6, 30))
    g = nw.by_group(clipped, accts)
    m = nw.monthly_change(g)
    assert len(m) == 6 and m["month"].iloc[0] == pd.Timestamp("2026-01-01")
    assert m["change"].sum() == pytest.approx(g["net_worth"].iloc[-1] - g["net_worth"].iloc[0], abs=0.01)
    rows = nw.account_rows(clipped, accts)
    assert [r["group_key"] for r in rows][0] == "cash"
    assert sum(r["balance"] for r in rows) == pytest.approx(g["net_worth"].iloc[-1], abs=0.05)


def test_flags_gaps_and_closed_accounts(demo):
    services.update_account("DEMO-CC", "include_in_networth", False)
    services.update_account("DEMO-SAV", "hidden", True)
    _, accts = services.read(nw.load)
    assert not {"DEMO-CC", "DEMO-SAV"} & set(accts["id"])
    _, picked = services.read(nw.load, ["DEMO-CC"])
    assert list(picked["id"]) == ["DEMO-CC"]  # explicit selection wins

    def drop_and_age(s):
        gap = (TODAY - dt.timedelta(days=20), TODAY - dt.timedelta(days=11))
        s.execute(delete(BalanceSnapshot).where(BalanceSnapshot.account_id == "DEMO-CHK",
                                                BalanceSnapshot.date.between(*gap)))
        # the auto loan stopped being returned 30 days ago and its last snapshot is from then
        s.execute(delete(BalanceSnapshot).where(BalanceSnapshot.account_id == "DEMO-AUTO",
                                                BalanceSnapshot.date > TODAY - dt.timedelta(days=30)))
        s.get(Account, "DEMO-AUTO").last_seen = dt.datetime.combine(TODAY - dt.timedelta(days=30), dt.time())
        return gap

    gap = services.write(drop_and_age)
    daily, _ = services.read(nw.load)
    chk = daily["DEMO-CHK"]
    assert (chk[pd.Timestamp(gap[0]):pd.Timestamp(gap[1])] == chk[pd.Timestamp(gap[0] - dt.timedelta(days=1))]).all()
    assert daily["DEMO-AUTO"].iloc[-1] == 0 and daily["DEMO-AUTO"][pd.Timestamp(TODAY - dt.timedelta(days=30))] < 0


def test_empty_database(tmp_path, monkeypatch, demo):
    services.write(lambda s: s.execute(delete(BalanceSnapshot)))
    daily, accts = services.read(nw.load)
    assert daily.empty and accts.empty
    g = nw.by_group(daily, accts)
    assert nw.kpis(g)["net_worth"] is None and nw.monthly_change(g).empty and nw.account_rows(daily, accts) == []

    from finance.app.main import create_app
    from finance.app.pages import networth as page
    create_app()
    kpis, comp, *_ = page.update("12m", None, None, None, "dark", 0)
    assert kpis[0].children[1].children == "-"
    hist, subtitle, clear_style = page.update_history(None, "12m", None, None, None, "dark", 0)
    assert "first sync" in subtitle and clear_style == {"display": "none"}


def test_page_callbacks(demo):
    from finance.app.main import create_app
    from finance.app.pages import networth as page

    create_app()
    assert page.layout()
    keys = page.GROUP_KEYS
    n = len(keys)
    for theme in ("light", "dark"):
        out = page.update("12m", None, None, None, theme, 0)
        kpis, comp, dates_style = out[:3]
        row_data = dict(zip(keys, out[3:3 + n]))
        titles = dict(zip(keys, out[3 + n:3 + 2 * n]))
        styles = dict(zip(keys, out[3 + 2 * n:3 + 3 * n]))
        assert len(kpis) == 4 and dates_style == {"display": "none"} and comp.data
        assert row_data["cash"] and styles["cash"] == {} and "Cash" in titles["cash"]
        # the main chart: full stacked net worth when nothing is selected
        hist, subtitle, clear_style = page.update_history(None, "12m", None, None, None, theme, 0)
        names = [t.name for t in hist.data if t.showlegend is not False]  # skip glow underlays/edges
        assert names[-1] == "Net worth" and "Cash" in names and "Loans and other debt" in names
        assert clear_style == {"display": "none"}
        # selecting an account swaps the chart to that account and shows the "Show all accounts" button
        acct = row_data["cash"][0]
        afig, asub, aclear = page.update_history(acct, "12m", None, None, None, theme, 0)
        assert afig.data and aclear == {} and acct["account"] in asub.children[0].children
    custom = page.update("custom", "2026-03-01", "2026-03-31", ["DEMO-CHK"], "light", 0)
    assert custom[2] == {}  # dates picker visible for a custom range
    assert sum(len(custom[3 + i]) for i in range(n)) == 1  # only DEMO-CHK across all group lists
    assert services.read(lambda s: s.scalar(select(Account.type).where(Account.id == "DEMO-CHK"))) == "checking"
