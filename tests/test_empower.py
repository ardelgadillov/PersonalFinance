import datetime as dt

import pytest
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from finance.config import Settings
from finance.db import get_engine
from finance.demo import ACCOUNTS_BY_ID, FakeSimpleFINClient, build_demo, simulate, write_empower_csv
from finance.categories import empower_mapping, load_category_file
from finance.importers.empower import (
    EmpowerImportError,
    import_empower_file,
    link_account,
    read_empower_csv,
    refresh_empower,
)
from finance.models import Account, Category, Rule, Transaction
from finance.sync import run_sync

TODAY = dt.date(2026, 11, 9)
EXPORT_UNTIL = TODAY - dt.timedelta(days=60)  # Empower export is 2 months older than today


def write(tmp_path, text, name="e.csv"):
    p = tmp_path / name
    p.write_text(text, encoding="utf-8")
    return p


def test_read_formats_and_sign_flip(tmp_path):
    lines = ["﻿Date,Account,Description,Category,Tags,Amount"]
    lines += [f"1/{d}/2024,Card,Store {d},Groceries,,\"$1,00{d}.50\"" for d in range(1, 7)]
    lines += ["1/9/2024,Checking,Paycheck,Paychecks/Salary,,(2500.00)", ",,,,,"]
    parsed = read_empower_csv(write(tmp_path, "\n".join(lines)))
    assert parsed.date_format == "%m/%d/%Y" and parsed.flipped_sign and parsed.skipped == 1
    assert parsed.rows[0].date == dt.date(2024, 1, 1) and parsed.rows[0].amount == -1001.50
    assert parsed.rows[-1].amount == 2500.00


def test_missing_columns(tmp_path):
    with pytest.raises(EmpowerImportError):
        read_empower_csv(write(tmp_path, "Date,Amount\n2024-01-01,5"))


@pytest.fixture
def demo(tmp_path):
    settings = Settings(simplefin_access_url=None, data_dir=tmp_path / "demo",
                        timezone="America/New_York", demo=True)
    build_demo(settings, today=TODAY)
    csv_path = tmp_path / "empower_old.csv"
    write_empower_csv(csv_path, simulate(TODAY), until=EXPORT_UNTIL)
    return settings, get_engine(settings.db_url), csv_path


def expected_category(world_txn, names):
    return empower_mapping(load_category_file()).get(world_txn.empower_category.lower())


def test_import_links_dedupes_and_categorizes(demo):
    settings, engine, csv_path = demo
    world = simulate(TODAY)
    with Session(engine) as s:
        r = import_empower_file(s, csv_path)
        s.commit()
        assert not r.flipped_sign and r.unmapped == []
        linked = {a.id: a.linked_empower_name for a in s.scalars(select(Account))}
        for acct_id in ("DEMO-CHK", "DEMO-SAV", "DEMO-CC", "DEMO-BRK", "DEMO-401K", "DEMO-AUTO"):
            assert linked[acct_id] == ACCOUNTS_BY_ID[acct_id].empower_name, acct_id
        # closed Wells Fargo account becomes history-only
        old = s.get(Account, "empower:wells-fargo-everyday-checking-ending-in-6614")
        assert old is not None and old.type == "checking"

        # No duplicates: every simulated transaction exists exactly once
        total = s.scalar(select(func.count()).select_from(Transaction))
        assert total == len(world.txns)

        names = {c.id: c.name for c in s.scalars(select(Category))}
        sf = s.scalars(select(Transaction).where(Transaction.source == "simplefin")).all()
        by_id = {t.id: t for t in world.txns}
        overlap = [t for t in sf if t.date <= EXPORT_UNTIL - dt.timedelta(days=3)]
        assert overlap and all(t.category_source == "empower" for t in overlap)

        recent = [t for t in sf if t.date > EXPORT_UNTIL + dt.timedelta(days=3)]
        assert s.scalar(select(func.count()).select_from(Rule).where(Rule.origin == "empower")) > 10
        by_rule = [t for t in recent if t.category_source == "rule"]
        assert len(by_rule) / len(recent) > 0.9
        correct = sum(names[t.category_id] == expected_category(by_id[t.external_id], names) for t in by_rule)
        assert correct / len(by_rule) > 0.95


def test_reimport_is_idempotent_and_keeps_manual_edits(demo):
    _, engine, csv_path = demo
    with Session(engine) as s:
        import_empower_file(s, csv_path)
        s.commit()
        t = s.scalars(select(Transaction).where(Transaction.source == "empower")
                      .order_by(Transaction.date)).first()
        t.category_id = s.scalar(select(Category.id).where(Category.name == "Travel"))
        t.category_source = "manual"
        s.commit()
        before = s.scalar(select(func.count()).select_from(Transaction))

        r = import_empower_file(s, csv_path)
        s.commit()
        assert r.staged_new == 0 and r.refresh.apply.inserted == 0 and r.refresh.apply.removed == 0
        assert s.scalar(select(func.count()).select_from(Transaction)) == before
        s.refresh(t)
        assert t.category_source == "manual"


def test_unlink_and_relink_moves_history(demo):
    _, engine, csv_path = demo
    with Session(engine) as s:
        import_empower_file(s, csv_path)
        s.commit()
        total = s.scalar(select(func.count()).select_from(Transaction))
        name = ACCOUNTS_BY_ID["DEMO-SAV"].empower_name

        link_account(s, name, None)
        refresh_empower(s)
        s.commit()
        hist = s.get(Account, "empower:ally-online-savings-ending-in-7710")
        n_hist = s.scalar(select(func.count()).select_from(Transaction)
                          .where(Transaction.account_id == hist.id))
        assert n_hist > 0  # all Ally rows now in a history-only account (overlap duplicated)

        link_account(s, name, "DEMO-SAV")
        refresh_empower(s)
        s.commit()
        assert s.get(Account, hist.id) is None
        assert s.scalar(select(func.count()).select_from(Transaction)) == total


def test_newer_export_and_sync_keep_single_copy(demo):
    settings, engine, csv_path = demo
    with Session(engine) as s:
        import_empower_file(s, csv_path)
        s.commit()
    full_csv = csv_path.with_name("empower_full.csv")
    write_empower_csv(full_csv, simulate(TODAY))
    with Session(engine) as s:
        import_empower_file(s, full_csv)
        s.commit()
    later = TODAY + dt.timedelta(days=5)
    run_sync(client=FakeSimpleFINClient(settings.timezone, today=later), engine=engine, settings=settings,
             now=dt.datetime.combine(later, dt.time(18)))
    with Session(engine) as s:
        assert s.scalar(select(func.count()).select_from(Transaction)) == len(simulate(later).txns)
        new = s.scalars(select(Transaction).where(Transaction.date > TODAY)).all()
        assert new and sum(t.category_id is not None for t in new) / len(new) > 0.9
