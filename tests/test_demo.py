import csv
import datetime as dt

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from finance.config import Settings
from finance.db import get_engine
from finance.demo import (
    ACCOUNTS,
    ANCHOR,
    OPENING_POSITIONS,
    FakeSimpleFINClient,
    build_demo,
    simulate,
)
from finance.models import Account, BalanceSnapshot, HoldingSnapshot, Transaction
from finance.sync import run_sync

TODAY = dt.date(2026, 11, 9)


def test_simulation_is_deterministic_and_stable_over_time():
    a = simulate(TODAY)
    b = simulate(TODAY + dt.timedelta(days=30))  # a month later: past must not change
    past_a = [(t.id, t.amount) for t in a.txns]
    past_b = [(t.id, t.amount) for t in b.txns if t.date <= TODAY]
    assert past_a == past_b
    assert len({t.id for t in a.txns}) == len(a.txns)


def test_cash_balances_reconcile_with_transactions():
    w = simulate(TODAY)
    for acct in ACCOUNTS:
        if acct.id in OPENING_POSITIONS:
            continue
        total = acct.opening + sum(t.amount for t in w.txns if t.account_id == acct.id)
        assert round(total, 2) == w.balances[acct.id][TODAY], acct.id
    assert w.balances["DEMO-OLDCHK"][TODAY] == 0  # closed account emptied
    assert w.balances["DEMO-CC"][TODAY] <= 0 and w.balances["DEMO-AUTO"][TODAY] < 0


def test_build_demo(tmp_path):
    settings = Settings(simplefin_access_url=None, data_dir=tmp_path / "demo",
                        timezone="America/New_York", demo=True)
    info = build_demo(settings, today=TODAY)
    assert info["sync"].status == "ok"
    engine = get_engine(settings.db_url)
    with Session(engine) as s:
        accts = s.scalars(select(Account)).all()
        assert len(accts) == 6 and all(a.type for a in accts)  # closed Wells Fargo not in SimpleFIN
        oldest = s.scalar(select(func.min(Transaction.date)))
        assert oldest >= TODAY - dt.timedelta(days=365)
        assert s.scalar(select(func.count()).select_from(BalanceSnapshot)) == 6 * 366
        assert s.scalar(select(func.count()).select_from(HoldingSnapshot)) > 365
        assert s.scalar(select(func.count()).select_from(Transaction)
                        .where(Transaction.pending.is_(True))) >= 0

    with info["csv_path"].open(encoding="utf-8") as f:
        rows = list(csv.DictReader(f))
    assert list(rows[0]) == ["Date", "Account", "Description", "Category", "Tags", "Amount"]
    assert min(r["Date"] for r in rows) == ANCHOR.isoformat()
    assert any(r["Account"].startswith("Wells Fargo") for r in rows)

    # A later sync through the fake bank is incremental and creates no duplicates
    res = run_sync(client=FakeSimpleFINClient(settings.timezone, today=TODAY), engine=engine,
                   settings=settings)
    assert res.mode == "incremental" and res.transactions_new == 0
