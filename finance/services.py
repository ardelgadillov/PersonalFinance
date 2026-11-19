"""Data access for the web app (plain dicts / DataFrames, no Dash imports).

Kept separate from the UI so it can be unit-tested and reused by later pages.
"""
from __future__ import annotations

import base64
import datetime as dt
import json
import logging
import re
import threading
import uuid
from dataclasses import dataclass
from pathlib import Path
from zoneinfo import ZoneInfo

from sqlalchemy import delete, func, select
from sqlalchemy.orm import Session

from finance.config import Settings, get_settings
from finance.db import get_engine, init_db
from finance.models import (
    ACCOUNT_TYPES,
    Account,
    BalanceSnapshot,
    Category,
    CategoryMap,
    EmpowerRow,
    HoldingSnapshot,
    SyncLog,
    Transaction,
    utcnow,
)

log = logging.getLogger(__name__)

_init_lock = threading.Lock()
_initialized: set[str] = set()

# Empower imports are heavy (parse + stage + link + apply). Serialize them so a
# double-fired upload (or two browser tabs) can't run two imports of the same file
# at once - that race was inserting the same empower_rows twice and crashing with a
# UNIQUE constraint error. With the lock the second import is a safe no-op re-import.
_empower_import_lock = threading.Lock()


def engine_for(settings: Settings | None = None):
    settings = settings or get_settings()
    settings.data_dir.mkdir(parents=True, exist_ok=True)
    engine = get_engine(settings.db_url)
    with _init_lock:
        if settings.db_url not in _initialized:
            init_db(engine)
            _initialized.add(settings.db_url)
    return engine


def session(settings: Settings | None = None) -> Session:
    return Session(engine_for(settings), expire_on_commit=False)


def read(fn, *args, **kwargs):
    """Run `fn(session, ...)` read-only, e.g. read(transactions.query, filter)."""
    with session() as s:
        return fn(s, *args, **kwargs)


def write(fn, *args, **kwargs):
    """Run `fn(session, ...)` and commit (rolls back if it raises)."""
    with session() as s:
        try:
            out = fn(s, *args, **kwargs)
        except Exception:
            s.rollback()
            raise
        s.commit()
        return out


def category_choices(settings: Settings | None = None) -> list[dict]:
    """[{id, name, group}] in display order."""
    with session(settings) as s:
        return [{"id": c.id, "name": c.name, "group": c.group}
                for c in s.scalars(select(Category).order_by(Category.sort_order, Category.name))]


def account_choices(settings: Settings | None = None) -> list[dict]:
    """[{id, label}] for filters (hidden accounts last)."""
    with session(settings) as s:
        accts = s.scalars(select(Account).order_by(Account.hidden, Account.org_name, Account.name)).all()
        return [{"id": a.id, "label": f"{a.label}" + (f" ({a.org_name})" if a.org_name else "")}
                for a in accts]


# --------------------------------------------------------------------------- time helpers
def to_local(ts: dt.datetime | None, settings: Settings | None = None) -> dt.datetime | None:
    if ts is None:
        return None
    tz = ZoneInfo((settings or get_settings()).timezone)
    return ts.replace(tzinfo=dt.timezone.utc).astimezone(tz)


def fmt_local(ts: dt.datetime | None, settings: Settings | None = None) -> str:
    loc = to_local(ts, settings)
    return loc.strftime("%Y-%m-%d %H:%M") if loc else ""


def humanize_age(ts: dt.datetime | None, now: dt.datetime | None = None) -> str:
    if ts is None:
        return "never"
    secs = int(((now or utcnow()) - ts).total_seconds())
    if secs < 90:
        return "just now"
    if secs < 3600:
        return f"{secs // 60} min ago"
    if secs < 86400 * 2:
        return f"{secs // 3600} h ago"
    return f"{secs // 86400} days ago"


# --------------------------------------------------------------------------- sync status
def last_sync(settings: Settings | None = None, ok_only: bool = False) -> SyncLog | None:
    with session(settings) as s:
        q = select(SyncLog).where(SyncLog.finished_at.is_not(None))
        if ok_only:
            q = q.where(SyncLog.status.in_(["ok", "partial"]))
        return s.scalars(q.order_by(SyncLog.id.desc()).limit(1)).first()


def sync_log_rows(settings: Settings | None = None, limit: int = 100) -> list[dict]:
    with session(settings) as s:
        logs = s.scalars(select(SyncLog).order_by(SyncLog.id.desc()).limit(limit)).all()
        return [{
            "id": r.id,
            "started": fmt_local(r.started_at, settings),
            "duration_s": round((r.finished_at - r.started_at).total_seconds(), 1) if r.finished_at else None,
            "mode": r.mode or "",
            "status": r.status,
            "requests": r.requests,
            "accounts": r.accounts,
            "new": r.transactions_new,
            "updated": r.transactions_updated,
            "errors": "; ".join(json.loads(r.errors_json)) if r.errors_json else "",
        } for r in logs]


# --------------------------------------------------------------------------- accounts
EDITABLE_ACCOUNT_FIELDS = {"display_name", "type", "include_in_networth", "hidden"}
SOURCE_LABELS = {"simplefin": "SimpleFIN", "empower": "Empower history", "manual": "Manual"}


def account_rows(settings: Settings | None = None) -> list[dict]:
    with session(settings) as s:
        counts = dict(s.execute(select(Transaction.account_id, func.count())
                                .group_by(Transaction.account_id)).all())
        first = dict(s.execute(select(Transaction.account_id, func.min(Transaction.date))
                               .group_by(Transaction.account_id)).all())
        accts = s.scalars(select(Account).order_by(Account.source.desc(), Account.org_name, Account.name)).all()
        return [{
            "id": a.id,
            "source": SOURCE_LABELS.get(a.source, a.source),
            "institution": a.org_name or "",
            "name": a.name,
            "display_name": a.display_name or "",
            "type": a.type or "",
            "include_in_networth": bool(a.include_in_networth),
            "hidden": bool(a.hidden),
            "balance": a.balance,
            "balance_date": fmt_local(a.balance_date, settings),
            "transactions": counts.get(a.id, 0),
            "since": first[a.id].isoformat() if first.get(a.id) else "",
            "linked_empower_name": a.linked_empower_name or "",
        } for a in accts]


def update_account(account_id: str, field: str, value, settings: Settings | None = None) -> None:
    if field not in EDITABLE_ACCOUNT_FIELDS:
        raise ValueError(f"Field {field!r} is not editable")
    if field == "type":
        value = value or None
        if value is not None and value not in ACCOUNT_TYPES:
            raise ValueError(f"Unknown account type {value!r}")
    elif field in ("include_in_networth", "hidden"):
        value = bool(value)
    elif field == "display_name":
        value = (value or "").strip() or None
    with session(settings) as s:
        acct = s.get(Account, account_id)
        if acct is None:
            raise ValueError(f"Unknown account {account_id!r}")
        setattr(acct, field, value)
        s.commit()


def untyped_account_count(settings: Settings | None = None) -> int:
    with session(settings) as s:
        return s.scalar(select(func.count()).select_from(Account).where(
            Account.type.is_(None), Account.hidden.is_(False))) or 0


def _local_today(settings: Settings | None = None) -> dt.date:
    return (to_local(utcnow(), settings) or utcnow()).date()


def _to_amount(value) -> float:
    """Parse a user-entered balance ('$1,234.50', '(500)' for a negative, etc.)."""
    text = str(value if value is not None else "").strip().replace(",", "").replace("$", "")
    negative = text.startswith("(") and text.endswith(")")
    if negative:
        text = text[1:-1]
    try:
        amount = float(text)
    except ValueError:
        raise ValueError("Enter a numeric balance, e.g. 1500 or -200.")
    return -amount if negative else amount


def create_manual_account(name: str, type_: str | None, balance, include_in_networth: bool = True,
                          settings: Settings | None = None) -> str:
    """Create a balance-only account whose balance you maintain by hand (no sync, no transactions)."""
    name = (name or "").strip()
    if not name:
        raise ValueError("Enter a name for the account.")
    type_ = type_ or None
    if type_ is not None and type_ not in ACCOUNT_TYPES:
        raise ValueError(f"Unknown account type {type_!r}")
    amount = _to_amount(balance)
    now, today = utcnow(), _local_today(settings)
    acct_id = "manual:" + uuid.uuid4().hex
    with session(settings) as s:
        s.add(Account(id=acct_id, source="manual", name=name, type=type_,
                      include_in_networth=bool(include_in_networth), hidden=False,
                      balance=amount, balance_date=now, first_seen=now, last_seen=now))
        s.add(BalanceSnapshot(account_id=acct_id, date=today, balance=amount))
        s.commit()
    return f"Added manual account '{name}' with a balance of ${amount:,.2f}."


def update_manual_balance(account_id: str, balance, settings: Settings | None = None) -> str:
    """Record today's balance for a manual account (overwrites an earlier entry made today)."""
    amount = _to_amount(balance)
    now, today = utcnow(), _local_today(settings)
    with session(settings) as s:
        acct = s.get(Account, account_id)
        if acct is None:
            raise ValueError(f"Unknown account {account_id!r}")
        if acct.source != "manual":
            raise ValueError("Only manual accounts have an editable balance.")
        snap = s.get(BalanceSnapshot, (account_id, today))
        if snap is None:
            s.add(BalanceSnapshot(account_id=account_id, date=today, balance=amount))
        else:
            snap.balance = amount
        acct.balance, acct.balance_date, acct.last_seen = amount, now, now
        s.commit()
        label = acct.label
    return f"Updated '{label}' balance to ${amount:,.2f}."


def delete_manual_account(account_id: str, settings: Settings | None = None) -> str:
    """Delete a manual account and its snapshots. Only manual accounts can be deleted."""
    with session(settings) as s:
        acct = s.get(Account, account_id)
        if acct is None:
            raise ValueError(f"Unknown account {account_id!r}")
        if acct.source != "manual":
            raise ValueError("Only manual accounts can be deleted. Hide synced accounts instead.")
        label = acct.label
        s.execute(delete(BalanceSnapshot).where(BalanceSnapshot.account_id == account_id))
        s.execute(delete(HoldingSnapshot).where(HoldingSnapshot.account_id == account_id))
        s.execute(delete(Transaction).where(Transaction.account_id == account_id))
        s.delete(acct)
        s.commit()
    return f"Deleted manual account '{label}'."


# --------------------------------------------------------------------------- Empower
HISTORY_ONLY = "__history__"


def empower_link_rows(settings: Settings | None = None) -> tuple[list[dict], list[dict]]:
    """Rows for the links grid and the dropdown options [{value, label}]."""
    from finance.importers.empower import suggest_links

    with session(settings) as s:
        stats = {name: (n, lo, hi) for name, n, lo, hi in s.execute(
            select(EmpowerRow.account_name, func.count(), func.min(EmpowerRow.date), func.max(EmpowerRow.date))
            .group_by(EmpowerRow.account_name))}
        sf_accounts = s.scalars(select(Account).where(Account.source == "simplefin")
                                .order_by(Account.org_name, Account.name)).all()
        options = [{"value": HISTORY_ONLY, "label": "History only (not linked)"}] + [
            {"value": a.id, "label": f"{a.org_name or ''} - {a.label}".strip(" -")} for a in sf_accounts]
        label = {o["value"]: o["label"] for o in options}
        linked = {a.linked_empower_name: a.id for a in sf_accounts if a.linked_empower_name}
        sugg = {sg.empower_name: sg for sg in suggest_links(s)} if stats else {}
        rows = []
        for name, (n, lo, hi) in sorted(stats.items()):
            sg = sugg.get(name)
            current = linked.get(name, HISTORY_ONLY)
            rows.append({
                "empower_name": name,
                "rows": n,
                "first": lo.isoformat(),
                "last": hi.isoformat(),
                "linked_to": label[current],
                "suggestion": (f"{label.get(sg.account_id, '?')} ({sg.matches} matching)"
                               if sg and sg.account_id else ""),
                "suggested_matches": sg.matches if sg else 0,
            })
        return rows, options


def set_empower_link(empower_name: str, target_label_or_id: str, settings: Settings | None = None) -> str:
    """Link/unlink and re-apply. Accepts an account id or a dropdown label."""
    from finance.importers.empower import link_account, refresh_empower

    _, options = empower_link_rows(settings)
    by_label = {o["label"]: o["value"] for o in options}
    target = by_label.get(target_label_or_id, target_label_or_id)
    with session(settings) as s:
        link_account(s, empower_name, None if target == HISTORY_ONLY else target)
        res = refresh_empower(s)
        s.commit()
    if res is None:
        return "Link saved."
    a = res.apply
    return (f"Link saved. Empower transactions: {a.inserted} added, {a.removed} removed, "
            f"{a.matched} matched SimpleFIN transactions.")


def auto_link_empower(settings: Settings | None = None) -> str:
    from finance.importers.empower import auto_link, refresh_empower

    with session(settings) as s:
        done = auto_link(s)
        refresh_empower(s)
        s.commit()
    return f"Auto-linked {len(done)} account(s)." if done else "No new confident matches."


def safe_filename(name: str) -> str:
    stem = re.sub(r"[^A-Za-z0-9._-]+", "_", Path(name or "upload.csv").name).strip("._") or "upload.csv"
    return stem if stem.lower().endswith(".csv") else stem + ".csv"


@dataclass
class UploadResult:
    ok: bool
    message: str
    details: list[str]


def import_empower_upload(contents: str, filename: str, settings: Settings | None = None) -> UploadResult:
    """Handle a dcc.Upload payload ('data:...;base64,<data>'): save to import dir and import."""
    from finance.importers.empower import EmpowerImportError, import_empower_file

    settings = settings or get_settings()
    try:
        data = base64.b64decode(contents.split(",", 1)[1])
    except (IndexError, ValueError):
        return UploadResult(False, "Could not read the uploaded file.", [])
    settings.import_dir.mkdir(parents=True, exist_ok=True)
    stamp = dt.datetime.now().strftime("%Y%m%d-%H%M%S")
    path = settings.import_dir / f"{stamp}_{safe_filename(filename)}"
    path.write_bytes(data)
    log.info("Empower import: %s (%d bytes) saved to %s", filename, len(data), path.name)
    with _empower_import_lock:  # never run two imports at once (see note above)
        with session(settings) as s:
            try:
                r = import_empower_file(s, path)
                s.commit()
            except EmpowerImportError as exc:
                s.rollback()
                log.warning("Empower import rejected (%s): %s", filename, exc)
                return UploadResult(False, f"Import failed: {exc}", [])
            except Exception:  # keep the UI responsive instead of returning an HTTP 500
                s.rollback()
                log.exception("Empower import failed (%s)", filename)
                return UploadResult(
                    False, "Import failed unexpectedly. See the application log for details.", [])
    log.info("Empower import ok (%s): %d read, %d new, %d updated, %d removed",
             filename, r.rows_read, r.staged_new, r.staged_updated, r.staged_removed)
    details = [
        f"{r.rows_read} rows read (date format {r.date_format}"
        f"{', amounts sign-flipped' if r.flipped_sign else ''}); "
        f"{r.staged_new} new, {r.staged_updated} updated, {r.staged_removed} removed.",
    ]
    if r.linked:
        details.append("Auto-linked: " + ", ".join(f"{x.empower_name} -> {x.account_label}" for x in r.linked))
    if r.refresh:
        a = r.refresh.apply
        details.append(f"Transactions: {a.inserted} added from Empower, {a.matched} matched SimpleFIN "
                       f"(categories copied), {a.covered_by_simplefin} already covered by SimpleFIN.")
        details.append(f"Rules learned: {r.refresh.rules_learned}.")
    if r.unmapped:
        details.append("Empower categories with no app category: "
                       + ", ".join(f"{n} ({c})" for n, c in r.unmapped))
    return UploadResult(True, f"Imported {path.name}", details)


def read_app_log(lines: int = 200, settings: Settings | None = None) -> str:
    """Return the last ``lines`` lines of the application log (newest last)."""
    settings = settings or get_settings()
    path = settings.log_file
    if not path.exists():
        return "No log file yet."
    try:
        with path.open(encoding="utf-8", errors="replace") as f:
            tail = f.readlines()[-lines:]
    except OSError as exc:  # pragma: no cover - depends on the filesystem
        return f"Could not read log: {exc}"
    return "".join(tail).strip() or "Log is empty."


# --------------------------------------------------------------------------- categories
def category_rows(settings: Settings | None = None) -> list[dict]:
    with session(settings) as s:
        counts = dict(s.execute(select(Transaction.category_id, func.count())
                                .group_by(Transaction.category_id)).all())
        maps: dict[int | None, list[str]] = {}
        for m in s.scalars(select(CategoryMap).order_by(CategoryMap.empower_category)):
            maps.setdefault(m.category_id, []).append(m.empower_category)
        return [{
            "id": c.id,
            "group": c.group,
            "parent": c.parent or "",
            "category": c.name,
            "sort_order": c.sort_order or 0,
            "transactions": counts.get(c.id, 0),
            "empower_categories": "; ".join(maps.get(c.id, [])),
        } for c in s.scalars(select(Category).order_by(Category.sort_order, Category.name))]


def uncategorized_count(settings: Settings | None = None) -> int:
    with session(settings) as s:
        return s.scalar(select(func.count()).select_from(Transaction)
                        .where(Transaction.category_id.is_(None))) or 0


def empower_category_rows(settings: Settings | None = None) -> list[dict]:
    """Every Empower category known (seeded or seen in imported data) with its mapping."""
    with session(settings) as s:
        names = {c.id: c.name for c in s.scalars(select(Category))}
        counts = dict(s.execute(select(EmpowerRow.category, func.count())
                                .group_by(EmpowerRow.category)).all())
        rows = [{
            "empower_category": m.empower_category,
            "rows": counts.get(m.empower_category, 0),
            "category": names.get(m.category_id, ""),
        } for m in s.scalars(select(CategoryMap))]
        return sorted(rows, key=lambda r: (r["category"] != "", -r["rows"], r["empower_category"].lower()))


def _category_id(s: Session, name: str | None) -> int | None:
    if not name:
        return None
    cid = s.scalar(select(Category.id).where(func.lower(Category.name) == name.strip().lower()))
    if cid is None:
        from finance.categories import CategoryError
        raise CategoryError(f"Unknown category {name!r}")
    return cid


def add_category(name: str, group: str, parent: str | None = None, settings: Settings | None = None) -> str:
    from finance.categories import add_category as _add
    with session(settings) as s:
        c = _add(s, name, group, parent)
        s.commit()
        return f"Added category '{c.name}' ({c.group})."


def edit_category(category_id: int, field: str, value, settings: Settings | None = None) -> str:
    from finance.categories import update_category
    with session(settings) as s:
        c = update_category(s, category_id, field, value)
        s.commit()
        return f"Saved '{c.name}'."


def category_usage(category_id: int, settings: Settings | None = None):
    from finance.categories import category_usage as _usage
    with session(settings) as s:
        return _usage(s, category_id)


def delete_category(category_id: int, move_to_name: str | None, settings: Settings | None = None) -> str:
    from finance.categories import delete_category as _delete, recategorize
    with session(settings) as s:
        name = s.get(Category, category_id).name if s.get(Category, category_id) else "?"
        move_to = _category_id(s, move_to_name)
        usage = _delete(s, category_id, move_to)
        recategorize(s)
        s.commit()
    msg = f"Deleted '{name}'."
    if move_to_name and (usage.manual or usage.user_rules or usage.empower_categories):
        msg += f" Moved its manual edits, rules and Empower mappings to '{move_to_name}'."
    return msg


def map_empower_category(empower_category: str, category_name: str | None,
                         settings: Settings | None = None) -> str:
    from finance.categories import recategorize, set_empower_mapping
    with session(settings) as s:
        set_empower_mapping(s, empower_category, _category_id(s, category_name))
        recategorize(s)
        s.commit()
    target = category_name or "(not mapped)"
    return f"Empower '{empower_category}' now maps to {target}; transactions recategorized."
