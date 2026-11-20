"""Fund menus: the funds an account's plan (401k, HSA, ...) lets you invest in, used by the optimizer."""
from __future__ import annotations

from sqlalchemy import and_, delete, func, select
from sqlalchemy.orm import Session

from finance.models import FundMenuItem, HoldingSnapshot


class FundMenuError(ValueError):
    pass


def tracked_symbols(s: Session) -> list[str]:
    """Every ticker worth caching prices for: all fund-menu symbols plus the symbols in each account's
    latest holdings. Used to warm the fund-price cache ahead of the optimizer."""
    menu = s.scalars(select(FundMenuItem.symbol)).all()
    latest = (select(HoldingSnapshot.account_id, func.max(HoldingSnapshot.date).label("d"))
              .group_by(HoldingSnapshot.account_id).subquery())
    held = s.scalars(
        select(HoldingSnapshot.symbol).join(
            latest, and_(HoldingSnapshot.account_id == latest.c.account_id, HoldingSnapshot.date == latest.c.d))
        .where(HoldingSnapshot.symbol.is_not(None))).all()
    return sorted({(x or "").upper() for x in (*menu, *held) if x and x.strip()})


def menu(s: Session, account_id: str) -> list[dict]:
    items = s.scalars(select(FundMenuItem).where(FundMenuItem.account_id == account_id)
                      .order_by(FundMenuItem.symbol)).all()
    return [{"id": i.id, "symbol": i.symbol, "name": i.name or ""} for i in items]


def add_funds(s: Session, account_id: str, funds: list[tuple[str, str | None]]) -> tuple[int, int]:
    """Add (symbol, name) pairs; an existing symbol only gets its name filled in. Returns (added, existing)."""
    if not account_id:
        raise FundMenuError("Choose an account first")
    current = {i.symbol: i for i in s.scalars(select(FundMenuItem).where(FundMenuItem.account_id == account_id))}
    added = existing = 0
    for symbol, name in funds:
        item = current.get(symbol)
        if item is None:
            item = FundMenuItem(account_id=account_id, symbol=symbol, name=name)
            s.add(item)
            current[symbol] = item
            added += 1
        else:
            existing += 1
            if name and not item.name:
                item.name = name
    return added, existing


def rename(s: Session, item_id: int, name: str | None) -> None:
    item = s.get(FundMenuItem, item_id)
    if item is None:
        raise FundMenuError("That fund is no longer in the menu")
    item.name = (name or "").strip() or None


def remove(s: Session, item_ids: list[int]) -> int:
    if not item_ids:
        return 0
    return s.execute(delete(FundMenuItem).where(FundMenuItem.id.in_(item_ids))).rowcount or 0


def _latest_holdings(s: Session, account_id: str) -> list[HoldingSnapshot]:
    day = s.scalar(select(func.max(HoldingSnapshot.date)).where(HoldingSnapshot.account_id == account_id))
    if day is None:
        return []
    return list(s.scalars(select(HoldingSnapshot).where(HoldingSnapshot.account_id == account_id,
                                                        HoldingSnapshot.date == day)))


def current_mix(s: Session, account_id: str, symbols: list[str]) -> tuple[dict[str, float], list[tuple[str, float]]]:
    """({symbol: weight} of the latest holdings that are in `symbols`, normalized to 1;
    [(holding, share of the account)] for holdings outside them)."""
    holdings = [h for h in _latest_holdings(s, account_id) if (h.market_value or 0) > 0]
    total = sum(h.market_value for h in holdings)
    if not total:
        return {}, []
    inside: dict[str, float] = {}
    outside: list[tuple[str, float]] = []
    wanted = set(symbols)
    for h in holdings:
        sym = (h.symbol or "").upper()
        if sym in wanted:
            inside[sym] = inside.get(sym, 0.0) + h.market_value
        else:
            outside.append((h.symbol or h.description or "?", h.market_value / total))
    inside_total = sum(inside.values())
    return ({k: v / inside_total for k, v in inside.items()} if inside_total else {}), outside


def add_current_holdings(s: Session, account_id: str) -> tuple[int, int]:
    funds = [((h.symbol or "").upper(), h.description) for h in _latest_holdings(s, account_id) if h.symbol]
    return add_funds(s, account_id, funds)
