"""Transactions: filtered listing, manual edits and categorization rules (session-based, no UI)."""
from __future__ import annotations

import datetime as dt
import re
from dataclasses import dataclass, field

from sqlalchemy import and_, func, or_, select
from sqlalchemy.orm import Session

from finance.categorize import apply_rules, load_rules, rule_matches
from finance.models import Account, Category, Rule, Transaction

UNCATEGORIZED = 0  # category filter value meaning "no category"
SOURCE_LABELS = {"manual": "Manual", "empower": "Empower", "rule": "Rule"}
TEXT_FIELDS = {"tags", "note"}
RULE_FIELDS = ["description", "payee", "memo"]
MATCH_TYPES = ["contains", "equals", "regex"]
USER_RULE_PRIORITY = 100  # learned rules use ~990, so your rules always run first

PERIODS = {
    "30d": "Last 30 days",
    "90d": "Last 90 days",
    "month": "This month",
    "last_month": "Last month",
    "ytd": "This year",
    "12m": "Last 12 months",
    "last_year": "Last year",
    "24m": "Last 24 months",
    "all": "All time",
    "custom": "Custom range",
}


class TransactionError(ValueError):
    pass


def _months_back(today: dt.date, n: int) -> dt.date:
    """First day of the month n months before today's month."""
    y, m = divmod(today.year * 12 + today.month - 1 - n, 12)
    return dt.date(y, m + 1, 1)


def period_range(period: str, today: dt.date | None = None) -> tuple[dt.date | None, dt.date | None]:
    today = today or dt.date.today()
    if period == "30d":
        return today - dt.timedelta(days=29), today
    if period == "90d":
        return today - dt.timedelta(days=89), today
    if period == "month":
        return today.replace(day=1), today
    if period == "last_month":
        last_prev = today.replace(day=1) - dt.timedelta(days=1)
        return last_prev.replace(day=1), last_prev
    if period == "ytd":
        return today.replace(month=1, day=1), today
    if period == "12m":
        return _months_back(today, 11), today
    if period == "24m":
        return _months_back(today, 23), today
    if period == "last_year":
        return dt.date(today.year - 1, 1, 1), dt.date(today.year - 1, 12, 31)
    return None, None


# --------------------------------------------------------------------------- listing
@dataclass
class TxnFilter:
    start: dt.date | None = None
    end: dt.date | None = None
    account_ids: list[str] = field(default_factory=list)
    category_ids: list[int] = field(default_factory=list)  # UNCATEGORIZED (0) = no category
    search: str | None = None
    exclude_transfers: bool = False  # drop confirmed transfer pairs and transfer-group categories
    exclude_hidden: bool = False  # drop hidden accounts (ignored when account_ids is given)
    exclude_types: tuple[str, ...] = ()  # drop these account types (ignored when account_ids is given)


def _amount_from(text: str) -> float | None:
    m = re.fullmatch(r"-?\$?\s*([0-9][0-9,]*(?:\.[0-9]{1,2})?)", text.strip())
    return float(m.group(1).replace(",", "")) if m else None


def _row(t: Transaction, accounts: dict[str, str], cats: dict[int, Category]) -> dict:
    c = cats.get(t.category_id)
    flags = (["Pending"] if t.pending else []) + (["Transfer pair"] if t.transfer_pair_id else [])
    return {
        "id": t.id,
        "date": t.date.isoformat(),
        "account": accounts.get(t.account_id, t.account_id),
        "account_id": t.account_id,
        "description": t.description or "",
        "memo": t.memo or "",
        "amount": t.amount,
        "category": c.name if c else "",
        "group": c.group if c else "",
        "how": SOURCE_LABELS.get(t.category_source or "", ""),
        "tags": t.tags or "",
        "note": t.note or "",
        "flags": ", ".join(flags),
        "source": "SimpleFIN" if t.source == "simplefin" else "Empower",
    }


def _lookups(s: Session) -> tuple[dict[str, str], dict[int, Category]]:
    return ({a.id: a.label for a in s.scalars(select(Account))},
            {c.id: c for c in s.scalars(select(Category))})


def query(s: Session, f: TxnFilter, limit: int | None = None) -> list[dict]:
    q = select(Transaction)
    if f.start:
        q = q.where(Transaction.date >= f.start)
    if f.end:
        q = q.where(Transaction.date <= f.end)
    if f.account_ids:
        q = q.where(Transaction.account_id.in_(f.account_ids))
    else:
        if f.exclude_hidden:
            q = q.where(Transaction.account_id.not_in(select(Account.id).where(Account.hidden.is_(True))))
        if f.exclude_types:
            q = q.where(Transaction.account_id.not_in(
                select(Account.id).where(Account.type.in_(f.exclude_types))))
    if f.exclude_transfers:
        transfer_cats = select(Category.id).where(Category.group == "transfer")
        q = q.where(Transaction.transfer_pair_id.is_(None),
                    or_(Transaction.category_id.is_(None), Transaction.category_id.not_in(transfer_cats)))
    if f.category_ids:
        ids = [c for c in f.category_ids if c != UNCATEGORIZED]
        conds = [Transaction.category_id.in_(ids)] if ids else []
        if UNCATEGORIZED in f.category_ids:
            conds.append(Transaction.category_id.is_(None))
        q = q.where(or_(*conds))
    if f.search and f.search.strip():
        text = f.search.strip()
        like = f"%{text}%"
        conds = [Transaction.description.ilike(like), Transaction.payee.ilike(like),
                 Transaction.memo.ilike(like), Transaction.tags.ilike(like), Transaction.note.ilike(like)]
        amount = _amount_from(text)
        if amount is not None:
            conds.append(and_(func.abs(Transaction.amount) >= amount - 0.005,
                              func.abs(Transaction.amount) <= amount + 0.005))
        q = q.where(or_(*conds))
    q = q.order_by(Transaction.date.desc(), Transaction.id.desc())
    if limit:
        q = q.limit(limit)
    accounts, cats = _lookups(s)
    return [_row(t, accounts, cats) for t in s.scalars(q)]


def get_rows(s: Session, ids: list[int]) -> list[dict]:
    accounts, cats = _lookups(s)
    return [_row(t, accounts, cats) for t in s.scalars(select(Transaction).where(Transaction.id.in_(ids)))]


def summarize(rows: list[dict]) -> dict:
    """Totals for a listing. Transfers are excluded from income and spending."""
    income = sum(r["amount"] for r in rows if r["group"] == "income")
    expense = sum(r["amount"] for r in rows if r["group"] == "expense")
    return {"count": len(rows), "income": round(income, 2), "spending": round(-expense, 2),
            "net": round(income + expense, 2), "uncategorized": sum(1 for r in rows if not r["category"])}


# --------------------------------------------------------------------------- edits
def set_category(s: Session, ids: list[int], category_id: int) -> int:
    """Manually categorize transactions (manual edits win over Empower and rules)."""
    if s.get(Category, category_id) is None:
        raise TransactionError("Unknown category")
    txns = s.scalars(select(Transaction).where(Transaction.id.in_(ids))).all()
    for t in txns:
        t.category_id, t.category_source = category_id, "manual"
    s.flush()
    return len(txns)


def reset_category(s: Session, ids: list[int]) -> int:
    """Drop manual categories so Empower mappings / rules decide again."""
    from finance.categories import recategorize

    txns = s.scalars(select(Transaction).where(Transaction.id.in_(ids),
                                               Transaction.category_source == "manual")).all()
    for t in txns:
        t.category_id, t.category_source = None, None
    s.flush()
    if txns:
        recategorize(s)
    return len(txns)


def update_text(s: Session, txn_id: int, field_name: str, value: str | None) -> None:
    if field_name not in TEXT_FIELDS:
        raise TransactionError(f"Field {field_name!r} is not editable")
    t = s.get(Transaction, txn_id)
    if t is None:
        raise TransactionError("Transaction not found")
    value = (value or "").strip()
    if field_name == "tags":
        value = ", ".join(dict.fromkeys(p.strip() for p in value.split(",") if p.strip()))
    setattr(t, field_name, value or None)
    s.flush()


# --------------------------------------------------------------------------- rules
def _user_rule():
    return or_(Rule.origin.is_(None), Rule.origin != "empower")


def _validate_rule(s: Session, pattern: str, field_name: str, match_type: str,
                   category_id: int | None, account_id: str | None) -> str:
    pattern = (pattern or "").strip()
    if not pattern:
        raise TransactionError("Pattern cannot be empty")
    if field_name not in RULE_FIELDS:
        raise TransactionError(f"Field must be one of {', '.join(RULE_FIELDS)}")
    if match_type not in MATCH_TYPES:
        raise TransactionError(f"Match type must be one of {', '.join(MATCH_TYPES)}")
    if match_type == "regex":
        try:
            re.compile(pattern)
        except re.error as exc:
            raise TransactionError(f"Invalid regular expression: {exc}") from None
    if category_id is None or s.get(Category, category_id) is None:
        raise TransactionError("Choose a category")
    if account_id and s.get(Account, account_id) is None:
        raise TransactionError("Unknown account")
    return pattern


@dataclass
class RulePreview:
    matches: int
    will_change: int
    keep_manual: int
    keep_empower: int
    samples: list[str]


def preview_rule(s: Session, pattern: str, field_name: str = "description", match_type: str = "contains",
                 category_id: int | None = None, account_id: str | None = None) -> RulePreview:
    """What a new rule would do. Earlier-priority rules are ignored (approximation)."""
    probe = Rule(pattern=(pattern or "").strip(), field=field_name, match_type=match_type,
                 account_id=account_id or None)
    p = RulePreview(0, 0, 0, 0, [])
    if not probe.pattern:
        return p
    for t in s.scalars(select(Transaction).order_by(Transaction.date.desc())):
        if not rule_matches(probe, t):
            continue
        p.matches += 1
        if len(p.samples) < 5:
            p.samples.append(f"{t.date:%Y-%m-%d}  {t.description}  {t.amount:,.2f}")
        if t.category_source == "manual":
            p.keep_manual += 1
        elif t.category_source == "empower":
            p.keep_empower += 1
        elif t.category_id != category_id:
            p.will_change += 1
    return p


def add_rule(s: Session, pattern: str, category_id: int, field_name: str = "description",
             match_type: str = "contains", account_id: str | None = None,
             priority: int = USER_RULE_PRIORITY) -> tuple[Rule, int]:
    """Add a rule and apply it. Returns (rule, transactions recategorized)."""
    pattern = _validate_rule(s, pattern, field_name, match_type, category_id, account_id)
    same_scope = Rule.account_id == account_id if account_id else Rule.account_id.is_(None)
    dup = s.scalar(select(Rule).where(_user_rule(), func.upper(Rule.pattern) == pattern.upper(),
                                      Rule.field == field_name, Rule.match_type == match_type, same_scope))
    if dup is not None:
        raise TransactionError(f"A rule for {pattern!r} already exists")
    rule = Rule(pattern=pattern, field=field_name, match_type=match_type, category_id=category_id,
                account_id=account_id or None, priority=int(priority), origin="user")
    s.add(rule)
    s.flush()
    return rule, apply_rules(s)


RULE_EDITABLE = {"pattern", "field", "match_type", "category_id", "account_id", "priority"}


def update_rule(s: Session, rule_id: int, field_name: str, value) -> int:
    """Edit one of your rules and re-apply rules. Returns transactions recategorized."""
    rule = s.get(Rule, rule_id)
    if rule is None:
        raise TransactionError("Rule not found")
    if rule.origin == "empower":
        raise TransactionError("Learned rules are rebuilt from Empower data; add your own rule to override")
    if field_name not in RULE_EDITABLE:
        raise TransactionError(f"Field {field_name!r} is not editable")
    if field_name == "priority":
        try:
            value = int(value)
        except (TypeError, ValueError):
            raise TransactionError("Priority must be a whole number") from None
    if field_name == "account_id":
        value = value or None
    new = {k: getattr(rule, k) for k in ("pattern", "field", "match_type", "category_id", "account_id")}
    if field_name in new:
        new[field_name] = value
        new["pattern"] = _validate_rule(s, new["pattern"], new["field"], new["match_type"],
                                        new["category_id"], new["account_id"])
        if field_name == "pattern":
            value = new["pattern"]
    setattr(rule, field_name, value)
    s.flush()
    return apply_rules(s)


def delete_rules(s: Session, ids: list[int]) -> tuple[int, int]:
    """Delete your rules (learned ones are skipped). Returns (deleted, transactions recategorized)."""
    rules = s.scalars(select(Rule).where(Rule.id.in_(ids), _user_rule())).all()
    for r in rules:
        s.delete(r)
    s.flush()
    return len(rules), apply_rules(s) if rules else 0


def rule_rows(s: Session, include_learned: bool = True) -> list[dict]:
    """Rules in evaluation order with how many transactions each currently categorizes."""
    rules = load_rules(s)
    uses: dict[int, int] = {}
    auto = s.scalars(select(Transaction).where(Transaction.category_source == "rule")).all()
    for t in auto:
        for r in rules:
            if rule_matches(r, t):
                uses[r.id] = uses.get(r.id, 0) + 1
                break
    accounts, cats = _lookups(s)
    return [{
        "id": r.id,
        "pattern": r.pattern,
        "field": r.field,
        "match_type": r.match_type,
        "category": cats[r.category_id].name if r.category_id in cats else "",
        "account": accounts.get(r.account_id, "") if r.account_id else "",
        "priority": r.priority,
        "uses": uses.get(r.id, 0),
        "origin": "Learned" if r.origin == "empower" else "You",
    } for r in rules if include_learned or r.origin != "empower"]
