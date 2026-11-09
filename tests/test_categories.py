import datetime as dt

import pytest
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from finance import db as dbmod
from finance import services
from finance.categories import CategoryError, load_category_file
from finance.config import get_settings
from finance.demo import build_demo
from finance.importers.empower import import_empower_file
from finance.models import Category, CategoryMap, Rule, Transaction

TODAY = dt.date(2026, 11, 9)


def test_invalid_seed_files(tmp_path):
    bad = tmp_path / "c.csv"
    bad.write_text("group,category\nspending,Food\n", encoding="utf-8")
    with pytest.raises(CategoryError, match="group must be"):
        load_category_file(bad)
    bad.write_text("group,category,parent,empower_categories\nexpense,Food,,Groceries\n"
                   "expense,food,,\n", encoding="utf-8")
    with pytest.raises(CategoryError, match="duplicate"):
        load_category_file(bad)


def test_app_default_seed_is_valid():
    from finance.config import PROJECT_ROOT
    defs = load_category_file(PROJECT_ROOT / "finance" / "default_categories.csv")
    assert len(defs) > 10 and {d.group for d in defs} == {"income", "expense", "transfer"}


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


def counts(engine):
    with Session(engine) as s:
        return dict(s.execute(select(Category.name, func.count(Transaction.id))
                              .join(Transaction, Transaction.category_id == Category.id)
                              .group_by(Category.name)).all())


def cat_id(engine, name):
    with Session(engine) as s:
        return s.scalar(select(Category.id).where(Category.name == name))


def test_seed_only_once(demo):
    with Session(demo) as s:
        n = s.scalar(select(func.count()).select_from(Category))
        assert n == len(load_category_file())
        assert s.get(CategoryMap, "Restaurants").category_id == cat_id(demo, "Dining")
    services.add_category("Coffee", "expense", "Food")
    dbmod._engines.clear()
    services._initialized.clear()
    services.engine_for()  # re-init must not re-seed or drop the new category
    assert cat_id(demo, "Coffee") is not None


def test_add_rename_edit_validation(demo):
    assert "Added" in services.add_category("  Coffee  shops ", "expense", "Food")
    cid = cat_id(demo, "Coffee shops")
    with pytest.raises(CategoryError, match="already exists"):
        services.add_category("coffee SHOPS", "expense")
    with pytest.raises(CategoryError, match="Group"):
        services.add_category("Other", "spending")
    with pytest.raises(CategoryError, match="empty"):
        services.add_category("   ", "expense")
    services.edit_category(cid, "name", "Coffee")
    services.edit_category(cid, "parent", "Food")
    services.edit_category(cid, "sort_order", "3")
    row = next(r for r in services.category_rows() if r["id"] == cid)
    assert (row["category"], row["parent"], row["sort_order"]) == ("Coffee", "Food", 3)
    with pytest.raises(CategoryError, match="already exists"):
        services.edit_category(cid, "name", "Dining")


def test_remap_empower_category_recategorizes(demo):
    before = counts(demo)
    services.map_empower_category("Online Services", "Entertainment")
    after = counts(demo)
    assert after.get("Subscriptions", 0) == 0
    assert after["Entertainment"] == before["Entertainment"] + before["Subscriptions"]
    services.map_empower_category("Online Services", None)
    assert services.uncategorized_count() > 0
    unmapped = [r for r in services.empower_category_rows() if not r["category"] and r["rows"]]
    assert unmapped[0]["empower_category"] == "Online Services"  # unmapped first


def test_delete_moves_manual_edits_rules_and_mappings(demo):
    dining, groceries = cat_id(demo, "Dining"), cat_id(demo, "Groceries")
    with Session(demo) as s:
        t = s.scalars(select(Transaction).where(Transaction.category_id == groceries).limit(1)).one()
        t.category_id, t.category_source = dining, "manual"
        s.add(Rule(field="description", pattern="BLUE BOTTLE", category_id=dining, origin="user"))
        s.commit()
        manual_id = t.id
    before = counts(demo)
    usage = services.category_usage(dining)
    assert usage.manual == 1 and usage.user_rules == 1 and usage.empower_categories == ["Restaurants"]

    services.delete_category(dining, "Groceries")
    after = counts(demo)
    assert "Dining" not in after
    assert after["Groceries"] == before["Groceries"] + before["Dining"]
    with Session(demo) as s:
        assert s.get(Transaction, manual_id).category_id == groceries
        assert s.get(CategoryMap, "Restaurants").category_id == groceries
        assert s.scalar(select(Rule.category_id).where(Rule.pattern == "BLUE BOTTLE")) == groceries


def test_delete_without_target_uncategorizes(demo):
    travel = cat_id(demo, "Travel")
    n = counts(demo)["Travel"]
    before = services.uncategorized_count()
    services.delete_category(travel, None)
    assert services.uncategorized_count() == before + n
    with pytest.raises(CategoryError):
        services.delete_category(cat_id(demo, "Groceries"), "Groceries")
