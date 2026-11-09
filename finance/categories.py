"""Categories live in the database and are managed in the app (Settings -> Categories).

finance/default_categories.csv is only used once, to seed a brand-new database.
Columns: group (income|expense|transfer), category, parent (optional label),
empower_categories (Empower categories mapped to it, separated by ';').

Precedence when categorizing a transaction: manual > Empower category > rules.
After any change here, call recategorize() so automatic assignments follow.
"""
from __future__ import annotations

import csv
from dataclasses import dataclass
from pathlib import Path

from sqlalchemy import func, select, update
from sqlalchemy.orm import Session

from finance.models import CATEGORY_GROUPS, Category, CategoryMap, Rule, Transaction


class CategoryError(ValueError):
    pass


# Backwards-compatible name used by the CLI.
CategoryFileError = CategoryError


@dataclass(frozen=True)
class CategoryDef:
    name: str
    group: str
    parent: str | None
    empower: tuple[str, ...]


def load_category_file(path: Path | None = None) -> list[CategoryDef]:
    """Read a seed file (default: finance/default_categories.csv)."""
    if path is None:
        from finance.config import get_settings
        path = get_settings().categories_seed_file
    path = Path(path)
    if not path.exists():
        raise CategoryError(f"Category file not found: {path}")
    defs: list[CategoryDef] = []
    names: set[str] = set()
    empower_seen: dict[str, str] = {}
    with path.open(encoding="utf-8-sig", newline="") as f:
        reader = csv.DictReader(f)
        cols = [c.strip().lower() for c in (reader.fieldnames or [])]
        if "group" not in cols or "category" not in cols:
            raise CategoryError(f"{path.name}: needs columns group, category, parent, empower_categories")
        for line_no, raw in enumerate(reader, start=2):
            row = {(k or "").strip().lower(): (v or "").strip() for k, v in raw.items()}
            name, group = row.get("category", ""), row.get("group", "").lower()
            if not name and not group:
                continue
            where = f"{path.name} line {line_no}"
            if not name:
                raise CategoryError(f"{where}: empty category name")
            if group not in CATEGORY_GROUPS:
                raise CategoryError(f"{where}: group must be one of {CATEGORY_GROUPS}, got {group!r}")
            if name.lower() in names:
                raise CategoryError(f"{where}: duplicate category {name!r}")
            names.add(name.lower())
            empower = tuple(e.strip() for e in row.get("empower_categories", "").split(";") if e.strip())
            for e in empower:
                if e.lower() in empower_seen:
                    raise CategoryError(
                        f"{where}: Empower category {e!r} is already mapped to {empower_seen[e.lower()]!r}")
                empower_seen[e.lower()] = name
            defs.append(CategoryDef(name, group, row.get("parent") or None, empower))
    if not defs:
        raise CategoryError(f"{path.name} has no categories")
    return defs


def empower_mapping(defs: list[CategoryDef]) -> dict[str, str]:
    """lower-case Empower category -> app category name."""
    return {e.lower(): d.name for d in defs for e in d.empower}


def seed_categories(s: Session, path: Path | None = None) -> bool:
    """Create categories and Empower mappings from the seed file if the DB has none yet."""
    if s.scalar(select(Category.id).limit(1)) is not None:
        return False
    defs = load_category_file(path)
    for order, d in enumerate(defs):
        c = Category(name=d.name, group=d.group, parent=d.parent, sort_order=order)
        s.add(c)
        s.flush()
        for e in d.empower:
            s.merge(CategoryMap(empower_category=e, category_id=c.id))
    s.flush()
    return True


# --------------------------------------------------------------------------- editing
def _clean_name(name: str | None) -> str:
    name = " ".join((name or "").split())
    if not name:
        raise CategoryError("Category name cannot be empty")
    if len(name) > 60:
        raise CategoryError("Category name is too long (max 60 characters)")
    return name


def _check_unique(s: Session, name: str, exclude_id: int | None = None) -> None:
    q = select(Category.id).where(func.lower(Category.name) == name.lower())
    if exclude_id is not None:
        q = q.where(Category.id != exclude_id)
    if s.scalar(q) is not None:
        raise CategoryError(f"A category named {name!r} already exists")


def _check_group(group: str | None) -> str:
    group = (group or "").strip().lower()
    if group not in CATEGORY_GROUPS:
        raise CategoryError(f"Group must be one of {', '.join(CATEGORY_GROUPS)}")
    return group


def add_category(s: Session, name: str, group: str, parent: str | None = None) -> Category:
    name = _clean_name(name)
    _check_unique(s, name)
    order = (s.scalar(select(func.max(Category.sort_order))) or 0) + 1
    c = Category(name=name, group=_check_group(group), parent=(parent or "").strip() or None, sort_order=order)
    s.add(c)
    s.flush()
    return c


def update_category(s: Session, category_id: int, field: str, value) -> Category:
    c = s.get(Category, category_id)
    if c is None:
        raise CategoryError("Unknown category")
    if field == "name":
        value = _clean_name(value)
        _check_unique(s, value, exclude_id=c.id)
    elif field == "group":
        value = _check_group(value)
    elif field == "parent":
        value = (value or "").strip() or None
    elif field == "sort_order":
        try:
            value = int(value)
        except (TypeError, ValueError):
            raise CategoryError("Order must be a whole number") from None
    else:
        raise CategoryError(f"Field {field!r} cannot be edited")
    setattr(c, field, value)
    s.flush()
    return c


@dataclass
class CategoryUsage:
    transactions: int
    manual: int
    user_rules: int
    empower_categories: list[str]


def category_usage(s: Session, category_id: int) -> CategoryUsage:
    return CategoryUsage(
        transactions=s.scalar(select(func.count()).select_from(Transaction)
                              .where(Transaction.category_id == category_id)) or 0,
        manual=s.scalar(select(func.count()).select_from(Transaction).where(
            Transaction.category_id == category_id, Transaction.category_source == "manual")) or 0,
        user_rules=s.scalar(select(func.count()).select_from(Rule).where(
            Rule.category_id == category_id, Rule.origin != "empower")) or 0,
        empower_categories=list(s.scalars(select(CategoryMap.empower_category)
                                          .where(CategoryMap.category_id == category_id))),
    )


def delete_category(s: Session, category_id: int, move_to_id: int | None = None) -> CategoryUsage:
    """Delete a category. Its manual transactions, your rules and Empower mappings move to
    `move_to_id` (or become uncategorized / unmapped / deleted when None)."""
    c = s.get(Category, category_id)
    if c is None:
        raise CategoryError("Unknown category")
    if move_to_id == category_id:
        raise CategoryError("Choose a different category to move transactions to")
    if move_to_id is not None and s.get(Category, move_to_id) is None:
        raise CategoryError("Unknown target category")
    usage = category_usage(s, category_id)

    s.execute(update(Transaction).where(Transaction.category_id == category_id,
                                        Transaction.category_source == "manual")
              .values(category_id=move_to_id, category_source="manual" if move_to_id else None))
    s.execute(update(Transaction).where(Transaction.category_id == category_id)
              .values(category_id=None, category_source=None))  # automatic ones are recomputed
    s.execute(update(CategoryMap).where(CategoryMap.category_id == category_id).values(category_id=move_to_id))
    s.execute(Rule.__table__.delete().where(Rule.category_id == category_id, Rule.origin == "empower"))
    if move_to_id:
        s.execute(update(Rule).where(Rule.category_id == category_id).values(category_id=move_to_id))
    else:
        s.execute(Rule.__table__.delete().where(Rule.category_id == category_id))
    s.delete(c)
    s.flush()
    return usage


def set_empower_mapping(s: Session, empower_category: str, category_id: int | None) -> None:
    if category_id is not None and s.get(Category, category_id) is None:
        raise CategoryError("Unknown category")
    m = s.get(CategoryMap, empower_category)
    if m is None:
        m = CategoryMap(empower_category=empower_category)
        s.add(m)
    m.category_id = category_id
    s.flush()


def recategorize(s: Session) -> int:
    """Re-apply Empower categories and rules. Returns transactions changed by rules."""
    from finance.categorize import apply_rules
    from finance.importers.empower import refresh_empower

    res = refresh_empower(s)
    return res.rules_applied if res else apply_rules(s)
