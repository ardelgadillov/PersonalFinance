"""Cash flow analytics and page callbacks on the demo household."""
import datetime as dt

import pytest
from sqlalchemy.orm import Session

from finance import cashflow as cf
from finance import db as dbmod
from finance import services
from finance import transactions as tx
from finance import transfers as tr
from finance.config import get_settings
from finance.demo import build_demo
from finance.importers.empower import import_empower_file

TODAY = dt.date(2026, 11, 9)
START, END = dt.date(2025, 1, 1), dt.date(2025, 12, 31)


@pytest.fixture
def demo(tmp_path, monkeypatch):
    monkeypatch.setenv("FINANCE_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("FINANCE_DEMO", "1")
    info = build_demo(get_settings(), today=TODAY)
    with Session(services.engine_for()) as s:
        import_empower_file(s, info["csv_path"])
        s.commit()
    yield
    for eng in dbmod._engines.values():
        eng.dispose()
    dbmod._engines.clear()
    services._initialized.clear()


def flows(**kw):
    return services.read(cf.load_flows, START, END, **kw)


def test_transfers_excluded_and_totals_consistent(demo):
    df = flows()
    assert not df.empty and set(df["kind"]) == {"income", "expense"}
    transfer_names = {c["name"] for c in services.category_choices() if c["group"] == "transfer"}
    assert not set(df["category"]) & transfer_names
    months = cf.month_range(START, END, df)
    assert len(months) == 12
    m = cf.monthly(df, months)
    k = cf.kpis(df, months)
    assert m["income"].sum() == pytest.approx(k["income"]) and m["spending"].sum() == pytest.approx(k["spending"])
    assert k["income"] > k["spending"] > 0 and 0 < k["savings_rate"] < 1
    assert k["avg_spending"] == pytest.approx(k["spending"] / 12, abs=0.01)
    cats = cf.by_category(df, "expense", 12)
    assert cats["total"].sum() == pytest.approx(k["spending"]) and cats["share"].max() <= 1
    assert cats["total"].is_monotonic_decreasing


def test_confirmed_pair_and_hidden_account_excluded(demo):
    pair = next(p for p in services.read(tr.suggest_pairs)
                if p.in_account == "Auto Loan" and START <= p.date <= END)
    paycheck = next(c["id"] for c in services.category_choices() if c["name"] == "Paycheck")
    services.write(tx.set_category, [pair.in_id], paycheck)  # mis-categorized incoming side
    before = cf.kpis(flows(), cf.month_range(START, END, flows()))
    services.write(tr.confirm_pair, pair.out_id, pair.in_id)  # categories kept, both sides excluded
    after = cf.kpis(flows(), cf.month_range(START, END, flows()))
    assert after["income"] == pytest.approx(before["income"] - pair.amount)
    assert after["spending"] == pytest.approx(before["spending"] - pair.amount)

    services.update_account("DEMO-CC", "hidden", True)
    assert "DEMO-CC" not in set(flows()["account_id"])
    assert set(flows(account_ids=["DEMO-CC"])["account_id"]) == {"DEMO-CC"}  # explicit selection wins


def test_trend_merchants_sankey(demo):
    df = flows()
    months = cf.month_range(START, END, df)
    top = cf.by_category(df, "expense", 12).iloc[0]["category"]
    trend = cf.category_trend(df, top, months)
    assert len(trend) == 12 and trend["amount"].sum() == pytest.approx(
        cf.by_category(df, "expense", 12).iloc[0]["total"])
    merchants = cf.top_merchants(df, 10)
    assert len(merchants) == 10 and merchants[0]["total"] >= merchants[-1]["total"]
    assert all("#" not in m["merchant"] for m in merchants)
    sk = cf.sankey(df, max_expense_nodes=6)
    into = sum(v for t, v in zip(sk["target"], sk["value"]) if t == 0)
    out = sum(v for s, v in zip(sk["source"], sk["value"]) if s == 0)
    assert into == pytest.approx(out, abs=0.05)
    assert sk["kinds"].count("expense") == 6 and "savings" in sk["kinds"]


def test_empty_range(demo):
    df = services.read(cf.load_flows, dt.date(1990, 1, 1), dt.date(1990, 3, 31))
    months = cf.month_range(dt.date(1990, 1, 1), dt.date(1990, 3, 31), df)
    assert df.empty and len(months) == 3
    assert cf.monthly(df, months)["income"].sum() == 0
    assert cf.kpis(df, months)["savings_rate"] is None
    assert cf.top_merchants(df) == [] and cf.sankey(df)["labels"] == []


def test_page_callbacks(demo):
    from finance.app.main import create_app
    from finance.app.pages import cashflow as page

    create_app()
    assert page.layout()
    for theme in ("light", "dark"):
        out = page.update("12m", None, None, None, theme, 0, None)
        kpis, monthly, cats, cat_style, sankey, merchants, options, trend_cat, dates_style, title, _ = out
        assert len(kpis) == 5 and monthly.data and cats.data and sankey.data and merchants
        assert trend_cat == options[0]["value"] and dates_style == {"display": "none"} and "month" in title
        assert page.update_trend(trend_cat, "12m", None, None, None, theme, 0).data
    empty = page.update("custom", "1990-01-01", "1990-02-01", None, "light", 0, "Groceries")
    assert empty[7] is None and empty[8] == {}
    assert page.pick_category({"points": [{"y": "Groceries"}]}) == "Groceries"
    assert tx.PERIODS["last_year"]


def test_daily_and_this_month_view(demo):
    first, last = cf.month_bounds(dt.date(2026, 2, 10))
    assert (first, last) == (dt.date(2026, 2, 1), dt.date(2026, 2, 28))
    assert cf.previous_month(dt.date(2026, 3, 31)) == (dt.date(2026, 2, 1), dt.date(2026, 2, 28))
    assert cf.previous_month(dt.date(2026, 1, 5)) == (dt.date(2025, 12, 1), dt.date(2025, 12, 31))

    lo, hi = dt.date(2026, 10, 1), dt.date(2026, 10, 31)
    df = services.read(cf.load_flows, lo, hi)
    d = cf.daily(df, lo, hi)
    assert len(d) == 31 and list(d["day_of_month"]) == list(range(1, 32))
    k = cf.kpis(df, cf.month_range(lo, hi, df))
    assert d["cum_net"].iloc[-1] == pytest.approx(k["net"])
    top = cf.by_category(df, "expense").iloc[0]
    assert cf.category_daily(df, top["category"], lo, hi)["amount"].sum() == pytest.approx(top["total"])

    from finance.app.main import create_app
    from finance.app.pages import cashflow as page
    create_app()
    out = page.update("month", None, None, None, "dark", 0, None)
    fig, title = out[1], out[9]
    assert title == "Income and spending by day"
    names = [t.name for t in fig.data if t.showlegend is not False]  # skip glow_lines underlays
    assert names[:2] == ["Income", "Spending"] and any(n.startswith("Net so far") for n in names[2:])
    assert "per day" in str(out[0][-1])
    if out[7]:
        trend = page.update_trend(out[7], "month", None, None, None, "dark", 0)
        assert trend.layout.xaxis.tickformat == "%b %d"
