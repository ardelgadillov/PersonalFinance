"""Manual (balance-only) accounts: create, update, delete and their effect on net worth."""
import datetime as dt

import pytest
from sqlalchemy import select

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


def _manual(name: str) -> dict:
    return next(r for r in services.account_rows() if r["name"] == name)


def test_create_manual_account_shows_in_networth(demo):
    services.create_manual_account("Rental condo", "other", "$250,000", include_in_networth=True)
    row = _manual("Rental condo")
    assert row["source"] == "Manual" and row["type"] == "other" and row["balance"] == pytest.approx(250_000)

    daily, accts = services.read(nw.load)
    # the balance is carried forward to the end of the range (manual accounts never "disconnect")
    assert daily[row["id"]].iloc[-1] == pytest.approx(250_000)
    assert dict(zip(accts["id"], accts["group"]))[row["id"]] == "other"


def test_manual_liability_counts_as_debt(demo):
    # entered as a positive number; a loan type always counts as -abs(balance)
    services.create_manual_account("Car loan", "loan", "18000")
    row = _manual("Car loan")
    daily, _ = services.read(nw.load)
    assert daily[row["id"]].iloc[-1] == pytest.approx(-18_000)


def test_update_manual_balance_overwrites_today(demo):
    services.create_manual_account("Cash box", "other", "100")
    row = _manual("Cash box")
    services.update_manual_balance(row["id"], "175")
    services.update_manual_balance(row["id"], "150")  # second edit same day replaces the first
    with services.session() as s:
        snaps = s.scalars(select(BalanceSnapshot).where(BalanceSnapshot.account_id == row["id"])).all()
    assert len(snaps) == 1 and snaps[0].balance == pytest.approx(150)
    assert _manual("Cash box")["balance"] == pytest.approx(150)


def test_delete_manual_account_removes_snapshots(demo):
    services.create_manual_account("Boat", "other", "9000")
    acct_id = _manual("Boat")["id"]
    services.delete_manual_account(acct_id)
    with services.session() as s:
        assert s.get(Account, acct_id) is None
        assert s.scalars(select(BalanceSnapshot).where(BalanceSnapshot.account_id == acct_id)).first() is None


def test_guards_reject_bad_input_and_synced_accounts(demo):
    with pytest.raises(ValueError):
        services.create_manual_account("", "other", "5")          # no name
    with pytest.raises(ValueError):
        services.create_manual_account("X", "other", "not a number")
    with pytest.raises(ValueError):
        services.create_manual_account("Y", "spaceship", "5")     # unknown type
    # synced accounts cannot be deleted or have their balance hand-edited
    with pytest.raises(ValueError):
        services.delete_manual_account("DEMO-CHK")
    with pytest.raises(ValueError):
        services.update_manual_balance("DEMO-CHK", "5")


def test_negative_and_parenthesised_amounts(demo):
    services.create_manual_account("Overdraft", "other", "(500)")   # accounting-style negative
    assert _manual("Overdraft")["balance"] == pytest.approx(-500)
    services.create_manual_account("IOU", "other", "-250")
    assert _manual("IOU")["balance"] == pytest.approx(-250)
