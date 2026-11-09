import copy
import datetime as dt
import json
from pathlib import Path

import pytest
from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import Session

from finance.config import Settings
from finance.models import Account, BalanceSnapshot, HoldingSnapshot, SyncLog, Transaction
from finance.simplefin.client import SimpleFINClient, SimpleFINError, parse_accounts_response
from finance.sync import run_sync

FIXTURE = json.loads((Path(__file__).parent / "fixtures" / "simplefin_accounts.json").read_text())
NOW = dt.datetime(2025, 11, 9, 16, 0, 0)


class FakeClient:
    """Returns the given payloads in order; afterwards, accounts with no transactions."""

    def __init__(self, *payloads):
        self.payloads = list(payloads)
        self.calls = []

    def get_accounts(self, start=None, end=None, pending=True, balances_only=False):
        self.calls.append((start, end))
        if self.payloads:
            return parse_accounts_response(self.payloads.pop(0))
        empty = copy.deepcopy(FIXTURE)
        empty["errors"] = []
        for a in empty["accounts"]:
            a["transactions"] = []
        return parse_accounts_response(empty)


@pytest.fixture
def env(tmp_path):
    settings = Settings(simplefin_access_url=None, data_dir=tmp_path, timezone="America/New_York")
    engine = create_engine(f"sqlite:///{(tmp_path / 'test.db').as_posix()}")
    return settings, engine


def sync(env, client, **kw):
    settings, engine = env
    return run_sync(client=client, engine=engine, settings=settings, now=kw.pop("now", NOW), **kw)


def test_parse_response():
    r = parse_accounts_response(FIXTURE)
    chk, brk = r.accounts
    assert chk.org_name == "Fake Bank" and chk.balance == 1520.75 and chk.available_balance == 1500.0
    assert [t.amount for t in chk.transactions] == [-45.20, 2500.0, -12.0]
    assert [t.pending for t in chk.transactions] == [False, False, True]
    assert chk.transactions[0].payee == "Whole Foods" and chk.transactions[0].memo is None
    assert brk.holdings[0].symbol == "VTI" and brk.holdings[0].market_value == 10000.0
    assert brk.holdings[1].symbol is None
    assert r.errors == ["Connection to Fake Bank may need attention"]


def test_client_url_parsing():
    c = SimpleFINClient("https://us%40er:p%3Ass@bridge.example/simplefin/")
    assert c._auth == ("us@er", "p:ss")
    assert c._base == "https://bridge.example/simplefin"
    with pytest.raises(SimpleFINError):
        SimpleFINClient("https://bridge.example/simplefin")


def test_initial_sync(env):
    client = FakeClient(FIXTURE)
    res = sync(env, client)
    assert res.mode == "initial" and res.status == "partial"
    assert res.requests == 2  # second window empty -> stop
    assert res.accounts == 2 and res.transactions_new == 3
    # windows are contiguous and go back in time
    assert client.calls[0][1] == NOW and client.calls[1][1] == client.calls[0][0]

    with Session(env[1]) as s:
        assert s.scalar(select(func.count()).select_from(Account)) == 2
        snap = s.get(BalanceSnapshot, ("ACT-checking", dt.date(2025, 11, 9)))
        assert snap.balance == 1520.75
        assert s.scalar(select(func.count()).select_from(HoldingSnapshot)) == 2
        log = s.scalars(select(SyncLog)).one()
        assert log.status == "partial" and len(json.loads(log.raw_paths)) == 2
    assert len(list((env[0].raw_dir).glob("*.json"))) == 2


def test_resync_is_idempotent_and_incremental(env):
    sync(env, FakeClient(FIXTURE))
    client = FakeClient(FIXTURE)
    res = sync(env, client, now=NOW + dt.timedelta(hours=6))
    assert res.mode == "incremental" and res.requests == 1
    assert res.transactions_new == 0 and res.transactions_updated == 0
    with Session(env[1]) as s:
        assert s.scalar(select(func.count()).select_from(Transaction)) == 3
        assert s.scalar(select(func.count()).select_from(HoldingSnapshot)) == 2  # same day replaced


def test_pending_replaced_by_posted_and_manual_edits_kept(env):
    sync(env, FakeClient(FIXTURE))
    with Session(env[1]) as s:
        t1 = s.scalars(select(Transaction).where(Transaction.external_id == "T1")).one()
        t1.note = "weekly shop"
        t1.category_id = 4
        t1.category_source = "manual"
        s.commit()

    later = copy.deepcopy(FIXTURE)
    for a in later["accounts"]:
        a["balance-date"] += 86400
    txns = later["accounts"][0]["transactions"]
    txns[0]["description"] = "WHOLE FOODS MARKET"  # bank cleaned up description
    txns[2] = {"id": "T3-posted", "posted": 1762750000, "amount": "-12.00", "description": "COFFEE SHOP"}
    res = sync(env, FakeClient(later), now=NOW + dt.timedelta(days=1))
    assert res.transactions_new == 1 and res.transactions_updated == 1

    with Session(env[1]) as s:
        ids = set(s.scalars(select(Transaction.external_id)))
        assert ids == {"T1", "T2", "T3-posted"}
        t1 = s.scalars(select(Transaction).where(Transaction.external_id == "T1")).one()
        assert t1.description == "WHOLE FOODS MARKET"
        assert (t1.note, t1.category_id, t1.category_source) == ("weekly shop", 4, "manual")
        # a new day -> a second balance snapshot
        assert s.scalar(select(func.count()).select_from(BalanceSnapshot)) == 4


def test_sync_error_is_logged(env):
    class Broken:
        def get_accounts(self, **_):
            raise SimpleFINError("SimpleFIN returned 403: access revoked or credentials invalid")

    res = sync(env, Broken())
    assert res.status == "error" and "403" in res.errors[0]
    with Session(env[1]) as s:
        assert s.scalars(select(SyncLog)).one().status == "error"
