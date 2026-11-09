"""Rule-based transaction categorization.

Precedence (highest first): manual edit > Empower category > rules.
Rules only (re)categorize transactions whose category_source is empty or "rule".
"""
from __future__ import annotations

import re
from functools import lru_cache

from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from finance.models import Rule, Transaction

# Tokens that carry no merchant information in bank descriptions.
NOISE_TOKENS = {
    "PPD", "ID", "ACH", "POS", "DEBIT", "CREDIT", "PURCHASE", "CARD", "CHECKCARD", "SQ", "TST",
    "WEB", "PMT", "PMTS", "PAYMENT", "ONLINE", "RECURRING", "INC", "LLC", "CO", "THE", "WWW",
    "COM", "STORE", "VISA", "MC", "DES", "INDN", "CCD", "WEBPMTS", "EBILL", "PAYPAL", "PP",
}
MAX_KEY_TOKENS = 3


def normalize_description(text: str | None) -> str:
    """Uppercase, strip punctuation, reference numbers and noise words."""
    if not text:
        return ""
    words = re.sub(r"[^A-Z0-9&' ]+", " ", text.upper().replace("'", "")).split()
    return " ".join(w for w in words if not any(c.isdigit() for c in w) and w not in NOISE_TOKENS)


def description_key(text: str | None) -> str:
    """Short merchant key used for learned rules, e.g. 'WHOLEFDS MKT #10234' -> 'WHOLEFDS MKT'."""
    return " ".join(normalize_description(text).split()[:MAX_KEY_TOKENS])


@lru_cache(maxsize=1024)
def _regex(pattern: str) -> re.Pattern | None:
    try:
        return re.compile(pattern, re.IGNORECASE)
    except re.error:
        return None


def rule_matches(rule: Rule, txn: Transaction) -> bool:
    if rule.account_id and rule.account_id != txn.account_id:
        return False
    value = getattr(txn, rule.field or "description", None) or ""
    pattern = rule.pattern or ""
    if not pattern:
        return False
    if rule.match_type == "regex":
        rx = _regex(pattern)
        return bool(rx and rx.search(value))
    if rule.match_type == "equals":
        return value.strip().upper() == pattern.strip().upper()
    # contains: case-insensitive on the raw text, or whole words on the normalized text
    if pattern.upper() in value.upper():
        return True
    p_norm = normalize_description(pattern)
    return bool(p_norm) and f" {p_norm} " in f" {normalize_description(value)} "


def load_rules(session: Session) -> list[Rule]:
    return list(session.scalars(select(Rule).order_by(Rule.priority, Rule.id)))


def categorize(txn: Transaction, rules: list[Rule]) -> int | None:
    for rule in rules:
        if rule_matches(rule, txn):
            return rule.category_id
    return None


def apply_rules(session: Session, only_uncategorized: bool = False) -> int:
    """Run rules over transactions that are not manually / Empower categorized.

    Returns the number of transactions whose category changed.
    """
    rules = load_rules(session)
    q = select(Transaction).where(or_(Transaction.category_source.is_(None),
                                      Transaction.category_source == "rule"))
    if only_uncategorized:
        q = q.where(Transaction.category_id.is_(None))
    changed = 0
    for txn in session.scalars(q):
        cat = categorize(txn, rules) if rules else None
        if cat != txn.category_id:
            txn.category_id = cat
            txn.category_source = "rule" if cat is not None else None
            changed += 1
    return changed
