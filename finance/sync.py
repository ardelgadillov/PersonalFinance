"""Pull data from SimpleFIN and upsert it into the local database.

- Initial sync: walk back in 45-day windows until a window returns no
  transactions or the request budget is used (Bridge allows ~24 requests/day).
- Incremental sync: one request covering the period since the last successful
  sync (with overlap), at most 45 days.
- Every sync stores one balance snapshot and one holdings snapshot per account
  per day (re-syncing the same day overwrites that day's snapshot).
"""
from __future__ import annotations

import datetime as dt
import json
import logging
from dataclasses import dataclass, field
from zoneinfo import ZoneInfo

from sqlalchemy import delete, func, select
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session

from finance.config import Settings, get_settings
from finance.db import get_engine, init_db
from finance.models import (
    Account,
    BalanceSnapshot,
    HoldingSnapshot,
    SyncLog,
    Transaction,
    utcnow,
)
from finance.simplefin.client import SFAccount, SFResponse, SimpleFINClient, SimpleFINError

log = logging.getLogger(__name__)

WINDOW_DAYS = 45  # Bridge recommends <= 45 days per request
INCREMENTAL_OVERLAP_DAYS = 10
MIN_INCREMENTAL_DAYS = 14
DEFAULT_MAX_REQUESTS = 12


@dataclass
class SyncResult:
    status: str
    mode: str
    requests: int = 0
    accounts: int = 0
    transactions_new: int = 0
    transactions_updated: int = 0
    errors: list[str] = field(default_factory=list)


def _local_date(ts: dt.datetime | None, tz: ZoneInfo, fallback: dt.datetime) -> dt.date:
    ts = ts or fallback
    return ts.replace(tzinfo=dt.timezone.utc).astimezone(tz).date()


def _archive_raw(settings: Settings, resp: SFResponse, started: dt.datetime, n: int) -> str:
    settings.raw_dir.mkdir(parents=True, exist_ok=True)
    path = settings.raw_dir / f"{started:%Y%m%d-%H%M%S}_{n:02d}.json"
    path.write_text(json.dumps(resp.raw, indent=1), encoding="utf-8")
    return str(path)


def _upsert_account(s: Session, sf: SFAccount, now: dt.datetime) -> Account:
    acct = s.get(Account, sf.id)
    if acct is None:
        acct = Account(id=sf.id, source="simplefin", name=sf.name, first_seen=now)
        s.add(acct)
    acct.name = sf.name
    acct.org_name = sf.org_name
    acct.org_domain = sf.org_domain
    acct.currency = sf.currency
    acct.last_seen = now
    return acct


def _write_snapshots(s: Session, acct: Account, sf: SFAccount, tz: ZoneInfo, now: dt.datetime) -> None:
    acct.balance = sf.balance
    acct.available_balance = sf.available_balance
    acct.balance_date = sf.balance_date or now
    day = _local_date(sf.balance_date, tz, now)

    if sf.balance is not None:
        s.merge(BalanceSnapshot(account_id=acct.id, date=day, balance=sf.balance,
                                available_balance=sf.available_balance))

    if sf.holdings:
        s.execute(delete(HoldingSnapshot).where(
            HoldingSnapshot.account_id == acct.id, HoldingSnapshot.date == day))
        for h in sf.holdings:
            s.add(HoldingSnapshot(
                account_id=acct.id, holding_id=h.id, date=day, symbol=h.symbol,
                description=h.description, currency=h.currency, shares=h.shares,
                cost_basis=h.cost_basis, market_value=h.market_value,
                purchase_price=h.purchase_price,
            ))


def _upsert_transactions(
    s: Session, acct: Account, sf: SFAccount, tz: ZoneInfo, now: dt.datetime,
    window: tuple[dt.date, dt.date], result: SyncResult,
) -> int:
    existing = {
        t.external_id: t
        for t in s.scalars(select(Transaction).where(
            Transaction.account_id == acct.id, Transaction.source == "simplefin",
            Transaction.external_id.in_([t.id for t in sf.transactions]),
        ))
    }
    for t in sf.transactions:
        fields = dict(
            date=_local_date(t.posted or t.transacted_at, tz, now),
            posted_at=t.posted,
            transacted_at=t.transacted_at,
            amount=t.amount,
            description=t.description,
            payee=t.payee,
            memo=t.memo,
            pending=t.pending,
        )
        row = existing.get(t.id)
        if row is None:
            s.add(Transaction(account_id=acct.id, source="simplefin", external_id=t.id, **fields))
            result.transactions_new += 1
        elif any(getattr(row, k) != v for k, v in fields.items()):
            for k, v in fields.items():
                setattr(row, k, v)
            result.transactions_updated += 1

    # Pending transactions often get a new id once posted: drop stale pending rows
    # in this window that the bank no longer reports.
    returned = {t.id for t in sf.transactions}
    stale = s.scalars(select(Transaction).where(
        Transaction.account_id == acct.id, Transaction.source == "simplefin",
        Transaction.pending.is_(True),
        Transaction.date >= window[0], Transaction.date <= window[1],
    )).all()
    for row in stale:
        if row.external_id not in returned:
            s.delete(row)
    return len(sf.transactions)


def _incremental_start(s: Session, now: dt.datetime) -> dt.datetime | None:
    has_txns = s.scalar(select(func.count()).select_from(Transaction)
                        .where(Transaction.source == "simplefin"))
    last_ok = s.scalar(select(func.max(SyncLog.finished_at))
                       .where(SyncLog.status.in_(["ok", "partial"])))
    if not has_txns or last_ok is None:
        return None
    start = last_ok - dt.timedelta(days=INCREMENTAL_OVERLAP_DAYS)
    start = min(start, now - dt.timedelta(days=MIN_INCREMENTAL_DAYS))
    return max(start, now - dt.timedelta(days=WINDOW_DAYS))


def run_sync(
    client: SimpleFINClient | None = None,
    engine: Engine | None = None,
    settings: Settings | None = None,
    full: bool = False,
    max_requests: int = DEFAULT_MAX_REQUESTS,
    now: dt.datetime | None = None,
) -> SyncResult:
    settings = settings or get_settings()
    if engine is None:
        settings.data_dir.mkdir(parents=True, exist_ok=True)
        engine = get_engine(settings.db_url)
    engine = init_db(engine)
    if client is None:
        if settings.demo:
            from finance.demo import FakeSimpleFINClient
            client = FakeSimpleFINClient(settings.timezone)
        else:
            client = SimpleFINClient(settings.simplefin_access_url or "")
    tz = ZoneInfo(settings.timezone)
    fixed_now = now is not None
    now = now or utcnow()

    with Session(engine, expire_on_commit=False) as s:
        log_row = SyncLog(started_at=now, status="running")
        s.add(log_row)
        s.commit()

        start = None if full else _incremental_start(s, now)
        mode = "initial" if start is None else "incremental"
        result = SyncResult(status="running", mode=mode)
        raw_paths: list[str] = []

        try:
            window_end = now
            window_start = start or now - dt.timedelta(days=WINDOW_DAYS)
            while True:
                resp = client.get_accounts(start=window_start, end=window_end, pending=True)
                result.requests += 1
                raw_paths.append(_archive_raw(settings, resp, now, result.requests))
                result.errors.extend(e for e in resp.errors if e not in result.errors)

                window = (_local_date(window_start, tz, now), _local_date(window_end, tz, now))
                n_txns = 0
                for sf in resp.accounts:
                    acct = _upsert_account(s, sf, now)
                    if result.requests == 1:
                        _write_snapshots(s, acct, sf, tz, now)
                    n_txns += _upsert_transactions(s, acct, sf, tz, now, window, result)
                if result.requests == 1:
                    result.accounts = len(resp.accounts)
                s.commit()

                if mode == "incremental" or n_txns == 0 or result.requests >= max_requests:
                    break
                window_end = window_start
                window_start = window_end - dt.timedelta(days=WINDOW_DAYS)

            # Categorize: Empower history/overlap first (if imported), then rules.
            from finance.categorize import apply_rules
            from finance.importers.empower import refresh_empower
            if refresh_empower(s) is None:
                apply_rules(s)
            s.commit()

            result.status = "partial" if result.errors else "ok"
        except SimpleFINError as exc:
            s.rollback()
            result.status = "error"
            result.errors.append(str(exc))
        except Exception as exc:  # keep a log row for unexpected failures too
            s.rollback()
            result.status = "error"
            result.errors.append(f"{type(exc).__name__}: {exc}")
            log.exception("Sync failed")

        log_row = s.merge(log_row)
        log_row.finished_at = now if fixed_now else utcnow()
        log_row.status = result.status
        log_row.mode = mode
        log_row.requests = result.requests
        log_row.accounts = result.accounts
        log_row.transactions_new = result.transactions_new
        log_row.transactions_updated = result.transactions_updated
        log_row.errors_json = json.dumps(result.errors) if result.errors else None
        log_row.raw_paths = json.dumps(raw_paths)
        s.commit()

    return result
