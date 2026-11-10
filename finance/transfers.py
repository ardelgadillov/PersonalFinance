"""Transfer matching: pair money leaving one of your accounts with the same amount arriving in another.

Pairs are only suggested; the user confirms them (both transactions get `transfer_pair_id` pointing at
each other and, optionally, a transfer category set as a manual edit) or dismisses them.
"""
from __future__ import annotations

import datetime as dt
from collections import defaultdict
from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.orm import Session

from finance.models import INVESTMENT_TYPES, Account, Category, Transaction, TransferDismissal

MATCH_DAYS = 3


class TransferError(ValueError):
    pass


@dataclass
class PairSuggestion:
    out_id: int
    in_id: int
    date: dt.date  # date money left
    days: int
    amount: float  # positive
    out_account: str
    in_account: str
    out_description: str
    in_description: str
    category_id: int | None  # suggested category for both sides (None = keep current)

    @property
    def key(self) -> str:
        return f"{self.out_id}-{self.in_id}"


def clean_pairs(s: Session) -> int:
    """Clear pair links whose partner was deleted or does not point back."""
    paired = {t.id: t for t in s.scalars(select(Transaction).where(Transaction.transfer_pair_id.is_not(None)))}
    broken = [t for t in paired.values()
              if (p := paired.get(t.transfer_pair_id)) is None or p.transfer_pair_id != t.id]
    for t in broken:
        t.transfer_pair_id = None
    s.flush()
    return len(broken)


def _guess_category(o: Transaction, i: Transaction, accounts: dict[str, Account],
                    cats: dict[int, Category]) -> int | None:
    for t in (o, i):
        c = cats.get(t.category_id)
        if c is not None and c.group == "transfer":
            return c.id
    transfer_cats = sorted((c for c in cats.values() if c.group == "transfer"),
                           key=lambda c: (c.sort_order or 0, c.name))
    if not transfer_cats:
        return None
    types = {getattr(accounts.get(o.account_id), "type", None), getattr(accounts.get(i.account_id), "type", None)}
    wanted = ("credit card" if "credit_card" in types else "invest" if types & INVESTMENT_TYPES else None)
    if wanted:
        for c in transfer_cats:
            if wanted in c.name.lower():
                return c.id
    for c in transfer_cats:
        if c.name.lower() == "transfer":
            return c.id
    return transfer_cats[0].id


def suggest_pairs(s: Session, days: int = MATCH_DAYS) -> list[PairSuggestion]:
    """Unpaired, posted transactions of opposite amount in different accounts within `days` days.

    Each transaction appears in at most one suggestion (closest dates win). Newest first.
    """
    clean_pairs(s)
    txns = s.scalars(select(Transaction).where(Transaction.transfer_pair_id.is_(None),
                                               Transaction.pending.is_(False),
                                               Transaction.amount != 0)).all()
    dismissed = {tuple(r) for r in s.execute(select(TransferDismissal.out_id, TransferDismissal.in_id))}
    buckets: dict[float, tuple[list[Transaction], list[Transaction]]] = defaultdict(lambda: ([], []))
    for t in txns:
        buckets[round(abs(t.amount), 2)][0 if t.amount < 0 else 1].append(t)

    candidates = []
    for outs, ins in buckets.values():
        if not outs or not ins:
            continue
        for o in outs:
            for i in ins:
                gap = abs((i.date - o.date).days)
                if o.account_id != i.account_id and gap <= days and (o.id, i.id) not in dismissed:
                    candidates.append((gap, o.date, o.id, i.id, o, i))
    candidates.sort(key=lambda c: c[:4])

    accounts = {a.id: a for a in s.scalars(select(Account))}
    cats = {c.id: c for c in s.scalars(select(Category))}
    used: set[int] = set()
    out: list[PairSuggestion] = []
    for gap, _, _, _, o, i in candidates:
        if o.id in used or i.id in used:
            continue
        used.update((o.id, i.id))
        out.append(PairSuggestion(
            out_id=o.id, in_id=i.id, date=o.date, days=gap, amount=round(-o.amount, 2),
            out_account=accounts[o.account_id].label if o.account_id in accounts else o.account_id,
            in_account=accounts[i.account_id].label if i.account_id in accounts else i.account_id,
            out_description=o.description, in_description=i.description,
            category_id=_guess_category(o, i, accounts, cats),
        ))
    return sorted(out, key=lambda p: (p.date, p.out_id), reverse=True)


def confirm_pair(s: Session, out_id: int, in_id: int, category_id: int | None = None) -> None:
    """Link two transactions as a transfer. A category, if given, is set on both as a manual edit."""
    o, i = s.get(Transaction, out_id), s.get(Transaction, in_id)
    if o is None or i is None:
        raise TransferError("Transaction not found (it may have been removed by a sync)")
    if o.id == i.id or o.account_id == i.account_id:
        raise TransferError("A transfer needs two different accounts")
    if abs(o.amount + i.amount) > 0.005:
        raise TransferError("Amounts do not offset each other")
    if o.transfer_pair_id or i.transfer_pair_id:
        raise TransferError("One of the transactions is already paired")
    if category_id is not None and s.get(Category, category_id) is None:
        raise TransferError("Unknown category")
    o.transfer_pair_id, i.transfer_pair_id = i.id, o.id
    if category_id is not None:
        for t in (o, i):
            t.category_id, t.category_source = category_id, "manual"
    s.flush()


def dismiss_pair(s: Session, out_id: int, in_id: int) -> None:
    if s.get(TransferDismissal, (out_id, in_id)) is None:
        s.add(TransferDismissal(out_id=out_id, in_id=in_id))
    s.flush()


def unpair(s: Session, txn_id: int) -> None:
    """Remove the pair link (categories are kept)."""
    t = s.get(Transaction, txn_id)
    if t is None or not t.transfer_pair_id:
        return
    partner = s.get(Transaction, t.transfer_pair_id)
    t.transfer_pair_id = None
    if partner is not None and partner.transfer_pair_id == t.id:
        partner.transfer_pair_id = None
    s.flush()


def paired_rows(s: Session, limit: int = 1000) -> list[dict]:
    """Confirmed pairs (one row per pair, keyed by the outgoing transaction), newest first."""
    clean_pairs(s)
    accounts = {a.id: a.label for a in s.scalars(select(Account))}
    cats = {c.id: c.name for c in s.scalars(select(Category))}
    outs = s.scalars(select(Transaction).where(Transaction.transfer_pair_id.is_not(None), Transaction.amount < 0)
                     .order_by(Transaction.date.desc(), Transaction.id.desc()).limit(limit)).all()
    partners = {t.id: t for t in s.scalars(select(Transaction).where(
        Transaction.id.in_([o.transfer_pair_id for o in outs])))} if outs else {}
    rows = []
    for o in outs:
        i = partners.get(o.transfer_pair_id)
        if i is None:
            continue
        rows.append({
            "id": o.id, "date": o.date.isoformat(), "amount": round(-o.amount, 2),
            "from_account": accounts.get(o.account_id, o.account_id),
            "to_account": accounts.get(i.account_id, i.account_id),
            "from_description": o.description, "to_description": i.description,
            "category": cats.get(o.category_id, ""),
        })
    return rows
