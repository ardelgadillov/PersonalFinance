"""Import Empower (Personal Capital) transaction CSV exports.

Flow:
1. read_empower_csv(): parse the file (date format and amount sign auto-detected).
2. stage_rows(): store raw rows in `empower_rows` (idempotent; a re-export replaces
   the rows in its date range).
3. auto_link(): link each Empower account name to the SimpleFIN account whose
   transactions match it (same amount, date within a few days).
4. apply_empower(): turn staged rows into `transactions`:
   - linked account -> rows go to the SimpleFIN account, but only *before* SimpleFIN's
     coverage starts (cutoff = first SimpleFIN transaction). Rows that match a
     SimpleFIN transaction are not duplicated; instead the SimpleFIN transaction
     receives the Empower category and tags.
   - unlinked account -> a history-only account "empower:<name>".
5. learn_rules(): from matched pairs, learn description -> category rules so new
   SimpleFIN transactions get categorized automatically.
"""
from __future__ import annotations

import csv
import datetime as dt
import difflib
import hashlib
import re
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from pathlib import Path

from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from finance.categorize import apply_rules, description_key
from finance.models import Account, Category, CategoryMap, EmpowerRow, Rule, Transaction

MATCH_MAX_DAYS = 3
CUTOFF_OVERLAP_DAYS = 5          # around the cutoff, drop Empower rows that match SimpleFIN
AUTO_LINK_MIN_MATCHES = 3
LEARN_MIN_COUNT = 2
LEARN_MIN_SHARE = 0.8

EXPENSE_HINTS = {"groceries", "restaurants", "gasoline/fuel", "general merchandise", "utilities",
                 "rent", "online services", "entertainment", "travel", "telephone",
                 "cable/satellite", "clothing/shoes", "healthcare/medical", "personal care",
                 "service charges/fees", "insurance"}
INCOME_HINTS = {"paychecks/salary"}

DATE_FORMATS = ["%Y-%m-%d", "%m/%d/%Y", "%m/%d/%y", "%Y/%m/%d", "%b %d, %Y", "%d/%m/%Y"]


class EmpowerImportError(ValueError):
    pass


# --------------------------------------------------------------------------- parsing
@dataclass
class ParsedRow:
    account_name: str
    date: dt.date
    description: str
    category: str | None
    tags: str | None
    amount: float


@dataclass
class ParsedCSV:
    rows: list[ParsedRow]
    date_format: str
    flipped_sign: bool
    skipped: int


def _parse_amount(text: str) -> float | None:
    s = (text or "").strip().replace("$", "").replace(",", "").replace(" ", "")
    if not s:
        return None
    neg = s.startswith("(") and s.endswith(")")
    s = s.strip("()")
    try:
        value = float(s)
    except ValueError:
        return None
    return -value if neg else value


def _detect_date_format(values: list[str]) -> str:
    for fmt in DATE_FORMATS:
        try:
            for v in values:
                dt.datetime.strptime(v, fmt)
            return fmt
        except ValueError:
            continue
    sample = values[0] if values else "?"
    raise EmpowerImportError(f"Unrecognized date format (example value: {sample!r})")


def read_empower_csv(path: str | Path) -> ParsedCSV:
    path = Path(path)
    with path.open(encoding="utf-8-sig", newline="") as f:
        reader = csv.DictReader(f)
        if not reader.fieldnames:
            raise EmpowerImportError("The file is empty")
        cols = {c.strip().lower(): c for c in reader.fieldnames if c}
        missing = [c for c in ("date", "account", "description", "amount") if c not in cols]
        if missing:
            raise EmpowerImportError(
                f"Missing column(s) {missing}; found {reader.fieldnames}. "
                "Expected: Date, Account, Description, Category, Tags, Amount")
        raw = list(reader)

    def get(r: dict, name: str) -> str:
        key = cols.get(name)
        return (r.get(key) or "").strip() if key else ""

    good = [r for r in raw if get(r, "date") and get(r, "account") and _parse_amount(get(r, "amount")) is not None]
    fmt = _detect_date_format([get(r, "date") for r in good])
    rows = [
        ParsedRow(
            account_name=get(r, "account"),
            date=dt.datetime.strptime(get(r, "date"), fmt).date(),
            description=get(r, "description"),
            category=get(r, "category") or None,
            tags=get(r, "tags") or None,
            amount=round(_parse_amount(get(r, "amount")), 2),
        )
        for r in good
    ]

    # Sign convention: we want money out < 0 (same as SimpleFIN).
    exp_pos = sum(1 for r in rows if (r.category or "").lower() in EXPENSE_HINTS and r.amount > 0)
    exp_neg = sum(1 for r in rows if (r.category or "").lower() in EXPENSE_HINTS and r.amount < 0)
    inc_pos = sum(1 for r in rows if (r.category or "").lower() in INCOME_HINTS and r.amount > 0)
    inc_neg = sum(1 for r in rows if (r.category or "").lower() in INCOME_HINTS and r.amount < 0)
    flip = (exp_pos + inc_neg) > 2 * (exp_neg + inc_pos) and (exp_pos + inc_neg) >= 5
    if flip:
        for r in rows:
            r.amount = -r.amount
    return ParsedCSV(rows=rows, date_format=fmt, flipped_sign=flip, skipped=len(raw) - len(good))


def _assign_ids(rows: list[ParsedRow]) -> list[tuple[str, ParsedRow]]:
    """Stable ids. Identical rows (e.g. two equal coffees on one day) get an occurrence index."""
    seen: Counter = Counter()
    out = []
    for r in sorted(rows, key=lambda r: (r.account_name, r.date, r.description, r.amount)):
        base = f"{r.account_name}|{r.date.isoformat()}|{r.description}|{r.amount:.2f}"
        seen[base] += 1
        digest = hashlib.sha1(f"{base}|{seen[base]}".encode()).hexdigest()[:20]
        out.append((f"emp-{digest}", r))
    return out


def stage_rows(s: Session, parsed: ParsedCSV, file_name: str | None = None) -> tuple[int, int, int]:
    """Upsert parsed rows into empower_rows. Returns (new, updated, removed).

    Rows previously imported for the same account within this file's date range that are
    no longer in the file are removed (the newer export wins).
    """
    incoming = dict(_assign_ids(parsed.rows))
    new = updated = removed = 0
    by_account: dict[str, list[dt.date]] = defaultdict(list)
    for r in parsed.rows:
        by_account[r.account_name].append(r.date)

    for name, dates in by_account.items():
        lo, hi = min(dates), max(dates)
        stale = s.scalars(select(EmpowerRow).where(
            EmpowerRow.account_name == name, EmpowerRow.date >= lo, EmpowerRow.date <= hi,
            EmpowerRow.id.not_in(list(incoming)))).all()
        for row in stale:
            s.delete(row)
            removed += 1

    existing = {r.id: r for r in s.scalars(select(EmpowerRow).where(EmpowerRow.id.in_(list(incoming))))}
    for rid, r in incoming.items():
        row = existing.get(rid)
        if row is None:
            s.add(EmpowerRow(id=rid, account_name=r.account_name, date=r.date, description=r.description,
                             category=r.category, tags=r.tags, amount=r.amount, file_name=file_name))
            new += 1
        elif (row.category, row.tags) != (r.category, r.tags):
            row.category, row.tags, row.file_name = r.category, r.tags, file_name
            updated += 1
    s.flush()
    return new, updated, removed


# --------------------------------------------------------------------------- categories
def ensure_category_map(s: Session) -> None:
    """Add a category_map entry for every new Empower category in the data.

    Mappings created at seed time (or edited in Settings) are kept. A new Empower category
    is mapped to an app category with the same name if there is one, otherwise left unmapped.
    """
    cats = {c.name.lower(): c.id for c in s.scalars(select(Category))}
    have = {m.lower() for m in s.scalars(select(CategoryMap.empower_category))}
    exact = {m.empower_category: m for m in s.scalars(select(CategoryMap))}
    for name in s.scalars(select(EmpowerRow.category).distinct()):
        if not name or name in exact:
            continue
        if name.lower() in have:  # same category with different capitalization: reuse its mapping
            twin = next(m for k, m in exact.items() if k.lower() == name.lower())
            s.add(CategoryMap(empower_category=name, category_id=twin.category_id))
        else:
            s.add(CategoryMap(empower_category=name, category_id=cats.get(name.lower())))
        have.add(name.lower())
    s.flush()


def category_lookup(s: Session) -> dict[str, int | None]:
    """lower-case Empower category -> app category id."""
    return {m.empower_category.lower(): m.category_id for m in s.scalars(select(CategoryMap))}


def unmapped_categories(s: Session) -> list[tuple[str, int]]:
    cmap = category_lookup(s)
    counts = Counter(c for c in s.scalars(select(EmpowerRow.category)) if c)
    return sorted(((c, n) for c, n in counts.items() if cmap.get(c.lower()) is None), key=lambda x: -x[1])


# --------------------------------------------------------------------------- matching / linking
def _match(rows: list[EmpowerRow], txns: list[Transaction], max_days: int = MATCH_MAX_DAYS
           ) -> list[tuple[EmpowerRow, Transaction]]:
    """One-to-one pairs with equal amount and dates within max_days (closest first)."""
    by_amount: dict[float, list[Transaction]] = defaultdict(list)
    for t in txns:
        by_amount[round(t.amount, 2)].append(t)
    used: set[int] = set()
    pairs = []
    for r in sorted(rows, key=lambda r: r.date):
        best: tuple[int, Transaction] | None = None
        for t in by_amount.get(round(r.amount, 2), ()):
            if t.id in used:
                continue
            d = abs((t.date - r.date).days)
            if d <= max_days and (best is None or d < best[0]):
                best = (d, t)
        if best:
            used.add(best[1].id)
            pairs.append((r, best[1]))
    return pairs


def _simplefin_txns(s: Session, account_id: str) -> list[Transaction]:
    return list(s.scalars(select(Transaction).where(
        Transaction.account_id == account_id, Transaction.source == "simplefin",
        Transaction.pending.is_(False))))


def _rows_by_account(s: Session) -> dict[str, list[EmpowerRow]]:
    out: dict[str, list[EmpowerRow]] = defaultdict(list)
    for r in s.scalars(select(EmpowerRow)):
        out[r.account_name].append(r)
    return out


@dataclass
class LinkSuggestion:
    empower_name: str
    account_id: str | None
    account_label: str | None
    matches: int
    rows: int
    name_similarity: float
    confident: bool


def suggest_links(s: Session) -> list[LinkSuggestion]:
    rows_by_name = _rows_by_account(s)
    accounts = list(s.scalars(select(Account).where(Account.source == "simplefin")))
    txns = {a.id: _simplefin_txns(s, a.id) for a in accounts}
    out = []
    for name, rows in sorted(rows_by_name.items()):
        scored = []
        for a in accounts:
            t = txns[a.id]
            if t:
                first = min(x.date for x in t) - dt.timedelta(days=MATCH_MAX_DAYS)
                n = len(_match([r for r in rows if r.date >= first], t))
            else:
                n = 0
            sim = difflib.SequenceMatcher(None, name.lower(), f"{a.org_name or ''} {a.name}".lower()).ratio()
            scored.append((n, sim, a))
        scored.sort(key=lambda x: (x[0], x[1]), reverse=True)
        if not scored:
            out.append(LinkSuggestion(name, None, None, 0, len(rows), 0.0, False))
            continue
        n, sim, best = scored[0]
        runner_up = scored[1][0] if len(scored) > 1 else 0
        confident = n >= AUTO_LINK_MIN_MATCHES and n >= 2 * runner_up
        if n == 0 and sim < 0.5:
            out.append(LinkSuggestion(name, None, None, 0, len(rows), sim, False))
        else:
            out.append(LinkSuggestion(name, best.id, f"{best.org_name or ''} {best.label}".strip(),
                                      n, len(rows), round(sim, 2), confident))
    return out


def link_account(s: Session, empower_name: str, account_id: str | None) -> None:
    """Link an Empower account name to a SimpleFIN account (None = keep as history-only)."""
    for a in s.scalars(select(Account).where(Account.linked_empower_name == empower_name)):
        a.linked_empower_name = None
    if account_id:
        acct = s.get(Account, account_id)
        if acct is None or acct.source != "simplefin":
            raise ValueError(f"Unknown SimpleFIN account id: {account_id}")
        acct.linked_empower_name = empower_name
    s.flush()


def auto_link(s: Session) -> list[LinkSuggestion]:
    """Link confident suggestions for Empower names and accounts that are not linked yet."""
    linked_names = set(s.scalars(select(Account.linked_empower_name).where(
        Account.linked_empower_name.is_not(None))))
    linked_accounts = set(s.scalars(select(Account.id).where(Account.linked_empower_name.is_not(None))))
    done = []
    for sug in sorted(suggest_links(s), key=lambda x: -x.matches):
        if (sug.confident and sug.empower_name not in linked_names
                and sug.account_id not in linked_accounts):
            link_account(s, sug.empower_name, sug.account_id)
            linked_names.add(sug.empower_name)
            linked_accounts.add(sug.account_id)
            done.append(sug)
    return done


# --------------------------------------------------------------------------- apply
ACCOUNT_TYPE_HINTS = [
    ("credit_card", ("credit", "card", "visa", "amex", "mastercard", "sapphire", "discover")),
    ("retirement", ("401", "403", "ira", "roth", "retire", "457", "tsp")),
    ("mortgage", ("mortgage",)),
    ("loan", ("loan", "auto finance")),
    ("savings", ("saving",)),
    ("checking", ("checking",)),
    ("brokerage", ("brokerage", "individual", "invest", "joint")),
    ("crypto", ("coinbase", "crypto", "bitcoin")),
]


def guess_account_type(name: str) -> str | None:
    low = name.lower()
    for typ, words in ACCOUNT_TYPE_HINTS:
        if any(w in low for w in words):
            return typ
    return None


def history_account_id(name: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")
    return f"empower:{slug}"


def _history_account(s: Session, name: str) -> Account:
    aid = history_account_id(name)
    acct = s.get(Account, aid)
    if acct is None:
        acct = Account(id=aid, source="empower", name=name, type=guess_account_type(name))
        s.add(acct)
        s.flush()
    return acct


@dataclass
class ApplyResult:
    inserted: int = 0
    updated: int = 0
    removed: int = 0
    covered_by_simplefin: int = 0
    matched: int = 0
    pairs: list[tuple[EmpowerRow, Transaction]] = field(default_factory=list, repr=False)


def apply_empower(s: Session) -> ApplyResult:
    res = ApplyResult()
    rows_by_name = _rows_by_account(s)
    cmap = category_lookup(s)
    links = {a.linked_empower_name: a for a in s.scalars(
        select(Account).where(Account.linked_empower_name.is_not(None)))}

    desired: dict[str, tuple[str, EmpowerRow]] = {}  # external_id -> (account_id, row)
    for name, rows in rows_by_name.items():
        acct = links.get(name)
        if acct is None:
            acct = _history_account(s, name)
            for r in rows:
                desired[r.id] = (acct.id, r)
            continue
        sf = _simplefin_txns(s, acct.id)
        cutoff = min((t.date for t in sf), default=None)
        if cutoff is None:
            for r in rows:
                desired[r.id] = (acct.id, r)
            continue
        window_start = cutoff - dt.timedelta(days=CUTOFF_OVERLAP_DAYS)
        pairs = _match([r for r in rows if r.date >= window_start], sf)
        matched = {r.id for r, _ in pairs}
        res.pairs.extend(pairs)
        for r in rows:
            if r.id in matched:
                continue
            if r.date >= cutoff:
                res.covered_by_simplefin += 1
                continue
            desired[r.id] = (acct.id, r)

    existing = {t.external_id: t for t in s.scalars(select(Transaction).where(Transaction.source == "empower"))}
    for ext_id, (account_id, r) in desired.items():
        cid = cmap.get(r.category.lower()) if r.category else None
        t = existing.pop(ext_id, None)
        if t is None:
            s.add(Transaction(
                account_id=account_id, source="empower", external_id=ext_id, date=r.date,
                amount=r.amount, description=r.description, payee=r.description, tags=r.tags,
                category_id=cid, category_source="empower" if cid else None,
            ))
            res.inserted += 1
            continue
        changed = False
        for attr, value in (("account_id", account_id), ("date", r.date), ("amount", r.amount),
                            ("description", r.description), ("payee", r.description)):
            if getattr(t, attr) != value:
                setattr(t, attr, value)
                changed = True
        if r.tags and not t.tags:
            t.tags, changed = r.tags, True
        if t.category_source != "manual" and (t.category_id, t.category_source) != (
                cid, "empower" if cid else None):
            if cid or t.category_source == "empower":
                t.category_id, t.category_source = cid, ("empower" if cid else None)
                changed = True
        res.updated += changed

    for t in existing.values():  # no longer wanted (e.g. now covered by SimpleFIN)
        s.delete(t)
        res.removed += 1

    # SimpleFIN transactions that match an Empower row inherit its category and tags.
    for r, t in res.pairs:
        cid = cmap.get(r.category.lower()) if r.category else None
        if t.category_source != "manual" and cid:
            t.category_id, t.category_source = cid, "empower"
        if r.tags and not t.tags:
            t.tags = r.tags
    res.matched = len(res.pairs)
    s.flush()

    # Remove history-only accounts that are now empty (their name got linked).
    for acct in s.scalars(select(Account).where(Account.source == "empower")):
        if acct.name in links and not s.scalar(select(Transaction.id).where(
                Transaction.account_id == acct.id).limit(1)):
            s.delete(acct)
    s.flush()
    return res


def learn_rules(s: Session, pairs: list[tuple[EmpowerRow, Transaction]]) -> int:
    """Replace learned rules with merchant-key rules derived from matched pairs."""
    cmap = category_lookup(s)
    votes: dict[str, Counter] = defaultdict(Counter)
    for r, t in pairs:
        cid = cmap.get(r.category.lower()) if r.category else None
        key = description_key(t.description)
        if cid and len(key) >= 3:
            votes[key][cid] += 1

    s.execute(delete(Rule).where(Rule.origin == "empower"))
    user_patterns = {p.upper() for p in s.scalars(select(Rule.pattern))}
    n = 0
    for key, counter in votes.items():
        cid, top = counter.most_common(1)[0]
        total = sum(counter.values())
        if total >= LEARN_MIN_COUNT and top / total >= LEARN_MIN_SHARE and key not in user_patterns:
            # Longer (more specific) keys first: 'AMAZON PRIME' before 'AMAZON'.
            s.add(Rule(field="description", pattern=key, match_type="contains", category_id=cid,
                       priority=1000 - len(key), origin="empower"))
            n += 1
    s.flush()
    return n


# --------------------------------------------------------------------------- orchestration
@dataclass
class RefreshResult:
    apply: ApplyResult
    rules_learned: int
    rules_applied: int


def refresh_empower(s: Session) -> RefreshResult | None:
    """Re-apply staged Empower rows, relearn rules and recategorize. Safe to call any time."""
    if s.scalar(select(EmpowerRow.id).limit(1)) is None:
        return None
    ensure_category_map(s)
    res = apply_empower(s)
    learned = learn_rules(s, res.pairs) if res.pairs else 0
    applied = apply_rules(s)
    return RefreshResult(res, learned, applied)


@dataclass
class ImportSummary:
    file: str
    rows_read: int
    rows_skipped: int
    date_format: str
    flipped_sign: bool
    staged_new: int
    staged_updated: int
    staged_removed: int
    linked: list[LinkSuggestion]
    suggestions: list[LinkSuggestion]
    refresh: RefreshResult | None
    unmapped: list[tuple[str, int]]


def import_empower_file(s: Session, path: str | Path, do_auto_link: bool = True) -> ImportSummary:
    path = Path(path)
    parsed = read_empower_csv(path)
    new, updated, removed = stage_rows(s, parsed, path.name)
    ensure_category_map(s)
    linked = auto_link(s) if do_auto_link else []
    refresh = refresh_empower(s)
    return ImportSummary(
        file=str(path), rows_read=len(parsed.rows), rows_skipped=parsed.skipped,
        date_format=parsed.date_format, flipped_sign=parsed.flipped_sign,
        staged_new=new, staged_updated=updated, staged_removed=removed,
        linked=linked, suggestions=suggest_links(s), refresh=refresh,
        unmapped=unmapped_categories(s),
    )
