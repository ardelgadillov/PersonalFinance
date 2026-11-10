"""Transactions listing, manual edits, rules and transfer matching (on the demo household)."""
import datetime as dt

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from finance import db as dbmod
from finance import services
from finance import transactions as tx
from finance import transfers as tr
from finance.categories import recategorize
from finance.config import get_settings
from finance.demo import build_demo
from finance.importers.empower import import_empower_file
from finance.models import Rule, Transaction

TODAY = dt.date(2026, 11, 9)


@pytest.fixture
def demo(tmp_path, monkeypatch):
    monkeypatch.setenv("FINANCE_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("FINANCE_DEMO", "1")
    info = build_demo(get_settings(), today=TODAY)
    engine = services.engine_for()
    with Session(engine) as s:
        import_empower_file(s, info["csv_path"])
        s.commit()
    yield engine
    for eng in dbmod._engines.values():
        eng.dispose()
    dbmod._engines.clear()
    services._initialized.clear()


def cat_id(name):
    return next(c["id"] for c in services.category_choices() if c["name"] == name)


def test_period_range():
    assert tx.period_range("30d", TODAY) == (dt.date(2026, 10, 11), TODAY)
    assert tx.period_range("ytd", TODAY) == (dt.date(2026, 1, 1), TODAY)
    assert tx.period_range("12m", TODAY)[0] == dt.date(2025, 12, 1)
    assert tx.period_range("all", TODAY) == (None, None)


def test_query_filters_and_summary(demo):
    all_rows = services.read(tx.query, tx.TxnFilter())
    assert all_rows and all_rows[0]["date"] >= all_rows[-1]["date"]
    f = tx.TxnFilter(start=dt.date(2026, 10, 1), end=dt.date(2026, 10, 31), account_ids=["DEMO-CHK"])
    rows = services.read(tx.query, f)
    assert rows and all(r["account_id"] == "DEMO-CHK" and r["date"].startswith("2026-10") for r in rows)
    assert all("STARBUCKS" in r["description"].upper()
               for r in services.read(tx.query, tx.TxnFilter(search="starbucks")))
    by_amount = services.read(tx.query, tx.TxnFilter(search="$2,150.00"))
    assert by_amount and all(abs(r["amount"]) == 2150 for r in by_amount)
    dining = services.read(tx.query, tx.TxnFilter(category_ids=[cat_id("Dining")]))
    assert dining and {r["category"] for r in dining} == {"Dining"}
    sm = tx.summarize(rows)
    assert sm["count"] == len(rows) and sm["income"] > 0 and sm["spending"] > 0
    assert sm["net"] == pytest.approx(sm["income"] - sm["spending"])


def test_manual_category_survives_recategorize_and_reset(demo):
    row = services.read(tx.query, tx.TxnFilter(search="starbucks"))[0]
    assert row["how"] == "Empower"
    services.write(tx.set_category, [row["id"]], cat_id("Travel"))
    services.write(recategorize)
    (after,) = services.read(tx.get_rows, [row["id"]])
    assert (after["category"], after["how"]) == ("Travel", "Manual")
    assert services.write(tx.reset_category, [row["id"]]) == 1
    (back,) = services.read(tx.get_rows, [row["id"]])
    assert (back["category"], back["how"]) == (row["category"], "Empower")


def test_tags_and_notes(demo):
    tid = services.read(tx.query, tx.TxnFilter())[0]["id"]
    services.write(tx.update_text, tid, "tags", " trip , work,trip ")
    services.write(tx.update_text, tid, "note", "  reimbursable ")
    (row,) = services.read(tx.get_rows, [tid])
    assert (row["tags"], row["note"]) == ("trip, work", "reimbursable")
    assert services.read(tx.query, tx.TxnFilter(search="reimbursable"))[0]["id"] == tid
    with pytest.raises(tx.TransactionError):
        services.write(tx.update_text, tid, "amount", "1")


def test_rules_crud_and_precedence(demo):
    with services.session() as s:  # make some SimpleFIN transactions rule-driven (no Empower category)
        for t in s.scalars(select(Transaction).where(Transaction.description.ilike("%STARBUCKS%"),
                                                     Transaction.source == "simplefin")):
            t.category_id, t.category_source = None, None
        s.commit()
    preview = services.read(tx.preview_rule, "starbucks", category_id=cat_id("Travel"))
    assert preview.matches > preview.will_change > 0 and preview.keep_empower > 0 and preview.samples

    rule, changed = services.write(tx.add_rule, "starbucks", cat_id("Travel"))
    assert changed == preview.will_change
    with pytest.raises(tx.TransactionError, match="already exists"):
        services.write(tx.add_rule, "STARBUCKS", cat_id("Travel"))
    with pytest.raises(tx.TransactionError, match="regular expression"):
        services.write(tx.add_rule, "(", cat_id("Travel"), match_type="regex")

    rows = {r["id"]: r for r in services.read(tx.rule_rows)}
    assert rows[rule.id]["uses"] == changed and rows[rule.id]["origin"] == "You"
    assert any(r["origin"] == "Learned" for r in rows.values())
    assert all(r["origin"] == "You" for r in services.read(tx.rule_rows, include_learned=False))

    services.write(tx.update_rule, rule.id, "category_id", cat_id("Shopping"))
    starbucks = [r for r in services.read(tx.query, tx.TxnFilter(search="starbucks")) if r["how"] == "Rule"]
    assert starbucks and {r["category"] for r in starbucks} == {"Shopping"}
    learned = next(r for r in rows.values() if r["origin"] == "Learned")
    with pytest.raises(tx.TransactionError, match="Learned"):
        services.write(tx.update_rule, learned["id"], "pattern", "x")

    assert services.write(tx.delete_rules, [rule.id, learned["id"]])[0] == 1  # learned rule kept
    with services.session() as s:
        assert s.get(Rule, learned["id"]) is not None


def test_transfer_suggestions_confirm_dismiss_unpair(demo):
    sugg = services.read(tr.suggest_pairs)
    assert len(sugg) > 50
    names = {c["id"]: c["name"] for c in services.category_choices()}
    cc = next(p for p in sugg if "AUTOPAY" in p.out_description)
    assert names[cc.category_id] == "Credit Card Payment" and cc.days <= tr.MATCH_DAYS
    ids = [i for p in sugg for i in (p.out_id, p.in_id)]
    assert len(ids) == len(set(ids))  # each transaction suggested at most once

    savings = next(p for p in sugg if p.in_account == "Online Savings")
    services.write(tr.confirm_pair, savings.out_id, savings.in_id, cat_id("Transfer"))
    rows = {r["id"]: r for r in services.read(tx.get_rows, [savings.out_id, savings.in_id])}
    assert all(r["category"] == "Transfer" and r["how"] == "Manual" and "Transfer pair" in r["flags"]
               for r in rows.values())
    with pytest.raises(tr.TransferError, match="already paired"):
        services.write(tr.confirm_pair, savings.out_id, savings.in_id)
    assert services.read(tr.paired_rows)[0]["id"] == savings.out_id

    services.write(tr.dismiss_pair, cc.out_id, cc.in_id)
    keys = {p.key for p in services.read(tr.suggest_pairs)}
    assert cc.key not in keys and savings.key not in keys

    services.write(tr.unpair, savings.in_id)
    assert services.read(tr.paired_rows) == []
    assert savings.key in {p.key for p in services.read(tr.suggest_pairs)}


def test_broken_pairs_are_cleaned(demo):
    p = services.read(tr.suggest_pairs)[0]
    services.write(tr.confirm_pair, p.out_id, p.in_id)
    with services.session() as s:  # e.g. a stale pending transaction removed by a sync
        s.delete(s.get(Transaction, p.in_id))
        s.commit()
    assert services.read(tr.paired_rows) == []
    assert services.write(tr.clean_pairs) == 1
    with services.session() as s:
        assert s.get(Transaction, p.out_id).transfer_pair_id is None
