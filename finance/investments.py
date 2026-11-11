"""Investment analytics from holding and balance snapshots (pandas, no UI).

Rules:
- Investment accounts: type brokerage / retirement / crypto, or any account that has holding snapshots.
  Hidden accounts are excluded unless explicitly selected.
- Portfolio value over time = the accounts' daily balances (finance.networth.load: carried over missed sync
  days until the account stops being seen), so it includes uninvested cash.
- Holdings are read from the latest snapshot on or before the end of the range, per account.
- Gain = market value - cost basis, only where the bank reports a cost basis (otherwise None).
- Price change in the period compares price (market value / shares) at the start and end, so deposits,
  withdrawals and buys do not count as performance.
- Return (performance vs a benchmark) chains daily price moves on the shares held the day before
  (sum(shares[t-1] * price[t]) / sum(shares[t-1] * price[t-1])), a time-weighted price return that ignores
  deposits and buys. Holdings without a share count cannot be priced and are left out of it.
"""
from __future__ import annotations

import datetime as dt

import pandas as pd
from sqlalchemy import select
from sqlalchemy.orm import Session

from finance import networth as nw
from finance.models import INVESTMENT_TYPES, Account, HoldingSnapshot

MAX_ACCOUNT_SERIES = 7  # the 8th categorical slot is "Other accounts"
OTHER_ACCOUNTS = "Other accounts"
HOLDING_COLUMNS = ["account_id", "holding_id", "date", "symbol", "description", "shares", "cost_basis",
                   "market_value"]


def investment_accounts(s: Session, account_ids: list[str] | None = None) -> list[dict]:
    """[{id, label, institution, type, slot}] in a stable order; `slot` (0-7) is the account's color, fixed by
    its position among ALL investment accounts so a filter never repaints the others."""
    with_holdings = set(s.scalars(select(HoldingSnapshot.account_id).distinct()))
    accts = [a for a in s.scalars(select(Account).order_by(Account.org_name, Account.name))
             if a.type in INVESTMENT_TYPES or a.id in with_holdings]
    out = []
    for i, a in enumerate(accts):
        if account_ids and a.id not in account_ids:
            continue
        if not account_ids and a.hidden:
            continue
        out.append({"id": a.id, "label": a.label, "institution": a.org_name or "", "type": a.type or "",
                    "slot": min(i, MAX_ACCOUNT_SERIES)})
    return out


def load_holdings(s: Session, account_ids: list[str]) -> pd.DataFrame:
    if not account_ids:
        return pd.DataFrame(columns=HOLDING_COLUMNS)
    rows = s.execute(select(HoldingSnapshot.account_id, HoldingSnapshot.holding_id, HoldingSnapshot.date,
                            HoldingSnapshot.symbol, HoldingSnapshot.description, HoldingSnapshot.shares,
                            HoldingSnapshot.cost_basis, HoldingSnapshot.market_value)
                     .where(HoldingSnapshot.account_id.in_(account_ids))).all()
    df = pd.DataFrame(rows, columns=HOLDING_COLUMNS)
    if df.empty:
        return df
    df["date"] = pd.to_datetime(df["date"])
    for col in ("shares", "cost_basis", "market_value"):
        df[col] = pd.to_numeric(df[col], errors="coerce")
    return df


def _price(mv, shares):
    return mv / shares if shares and pd.notna(shares) and pd.notna(mv) and shares != 0 else None


def snapshot_at(h: pd.DataFrame, day: dt.date | None, first_on_or_after: bool = False) -> pd.DataFrame:
    """Holdings of each account on its latest snapshot <= day (or earliest >= day)."""
    if h.empty:
        return h
    if day is None:
        sub = h
        pick = "min" if first_on_or_after else "max"
    else:
        ts = pd.Timestamp(day)
        sub = h[h["date"] >= ts] if first_on_or_after else h[h["date"] <= ts]
        pick = "min" if first_on_or_after else "max"
    if sub.empty:
        return sub
    dates = sub.groupby("account_id")["date"].transform(pick)
    return sub[sub["date"] == dates]


def holding_rows(h: pd.DataFrame, accounts: list[dict], start: dt.date | None,
                 end: dt.date | None) -> list[dict]:
    """One row per holding at the end of the range, largest market value first."""
    now = snapshot_at(h, end)
    if now.empty:
        return []
    then = snapshot_at(h[h["date"] <= pd.Timestamp(end)] if end else h, start, first_on_or_after=True)
    start_price = {(r.account_id, r.holding_id): _price(r.market_value, r.shares) for r in then.itertuples()}
    label = {a["id"]: a["label"] for a in accounts}
    total = float(now["market_value"].sum()) or 1.0
    rows = []
    for r in now.itertuples():
        mv = float(r.market_value) if pd.notna(r.market_value) else 0.0
        cost = float(r.cost_basis) if pd.notna(r.cost_basis) and r.cost_basis else None
        price = _price(r.market_value, r.shares)
        p0 = start_price.get((r.account_id, r.holding_id))
        rows.append({
            "id": f"{r.account_id}|{r.holding_id}", "account_id": r.account_id, "holding_id": r.holding_id,
            "symbol": r.symbol or "", "description": r.description or "", "account": label.get(r.account_id, ""),
            "shares": round(float(r.shares), 4) if pd.notna(r.shares) else None,
            "price": round(price, 4) if price is not None else None,
            "market_value": round(mv, 2),
            "cost_basis": round(cost, 2) if cost is not None else None,
            "gain": round(mv - cost, 2) if cost is not None else None,
            "gain_pct": (mv - cost) / cost if cost else None,
            "weight": mv / total,
            "price_change": (price / p0 - 1) if price is not None and p0 else None,
            "as_of": r.date.date().isoformat(),
        })
    rows.sort(key=lambda x: -x["market_value"])
    return rows


def symbol_of(row: dict) -> str:
    """Display key of a holding: its symbol, or its name when the bank gives no symbol."""
    return row["symbol"] or row["description"]


def allocation(rows: list[dict], by: str = "symbol", max_items: int = 12) -> pd.DataFrame:
    """label, key, value, share by symbol (same symbol across accounts summed) or by account; small ones folded
    into "Other (n)" (key None). key = the symbol, or the account id."""
    if not rows:
        return pd.DataFrame(columns=["label", "key", "value", "share"])
    df = pd.DataFrame(rows)
    if by == "account":
        df["key"], df["label"] = df["account_id"], df["account"]
    else:
        df["key"] = df["label"] = [symbol_of(r) for r in rows]
    g = df.groupby(["key", "label"])["market_value"].sum().sort_values(ascending=False)
    g = g[g > 0].reset_index()
    if len(g) > max_items:
        rest = g.iloc[max_items - 1:]
        g = pd.concat([g.iloc[:max_items - 1], pd.DataFrame([{"key": None, "label": f"Other ({len(rest)})",
                                                               "market_value": rest["market_value"].sum()}])])
    out = g.rename(columns={"market_value": "value"})[["label", "key", "value"]].reset_index(drop=True)
    out["share"] = out["value"] / out["value"].sum() if out["value"].sum() else 0.0
    return out


def focus_holdings(h: pd.DataFrame, symbol: str | None) -> pd.DataFrame:
    """Only the snapshots of one symbol (or name, for holdings without a symbol); all when symbol is None."""
    if not symbol or h.empty:
        return h
    key = h["symbol"].where(h["symbol"].notna() & (h["symbol"] != ""), h["description"])
    return h[key == symbol]


def _daily(per: pd.DataFrame, index: pd.DatetimeIndex) -> pd.DataFrame:
    """Snapshot-day values (index date, one column per key) -> every day of `index`: carried over missed days
    up to each column's last snapshot, NaN before the first and after the last."""
    full = per.reindex(per.index.union(index)).sort_index()
    filled = full.ffill()
    for col in full.columns:
        last = full[col].last_valid_index()
        if last is not None:
            filled.loc[filled.index > last, col] = float("nan")
    return filled.reindex(index)


def cost_daily(h: pd.DataFrame, index: pd.DatetimeIndex) -> pd.Series | None:
    """Total cost basis per day over `index` (carried over missed days); None when no cost basis is reported."""
    if h.empty or not h["cost_basis"].notna().any() or not len(index):
        return None
    per = h.groupby(["date", "account_id"])["cost_basis"].sum(min_count=1).unstack()
    return _daily(per, index).fillna(0.0).sum(axis=1)


def holding_values(h: pd.DataFrame, index: pd.DatetimeIndex, accounts: list[dict]) -> pd.DataFrame:
    """Like value_by_account, but the market value of the given holdings (e.g. one symbol) per account."""
    if h.empty or not len(index):
        return pd.DataFrame(index=index)
    per = h.groupby(["date", "account_id"])["market_value"].sum(min_count=1).unstack()
    daily = _daily(per, index).fillna(0.0)
    return value_by_account(daily, accounts)


def return_index(h: pd.DataFrame, index: pd.DatetimeIndex) -> pd.Series | None:
    """Cumulative time-weighted price return over `index` (0 on its first day); None without priced holdings."""
    if h.empty or not len(index):
        return None
    priced = h[(h["shares"] > 0) & h["market_value"].notna()]
    if priced.empty:
        return None
    key = priced["account_id"] + "|" + priced["holding_id"]
    shares = priced.assign(key=key).pivot_table(index="date", columns="key", values="shares", aggfunc="last")
    price = (priced["market_value"] / priced["shares"]).to_frame("p").assign(key=key, date=priced["date"])
    price = price.pivot_table(index="date", columns="key", values="p", aggfunc="last")
    days = pd.date_range(min(shares.index.min(), index.min()), index.max(), freq="D")
    shares, price = _daily(shares, days), _daily(price, days)
    held = shares.shift(1)
    ok = held.notna() & price.notna() & price.shift(1).notna()
    num = (held * price).where(ok).sum(axis=1)
    den = (held * price.shift(1)).where(ok).sum(axis=1)
    step = (num / den).where(den > 0, 1.0).reindex(index).fillna(1.0)
    step.iloc[0] = 1.0  # the window starts at 0%
    if not ok.reindex(index).any(axis=None):
        return None
    return step.cumprod() - 1


def value_by_account(daily: pd.DataFrame, accounts: list[dict]) -> pd.DataFrame:
    """Columns = account label (accounts past MAX_ACCOUNT_SERIES folded into OTHER_ACCOUNTS) + total."""
    out = pd.DataFrame(index=daily.index)
    for a in accounts:
        if a["id"] not in daily:
            continue
        name = OTHER_ACCOUNTS if a["slot"] >= MAX_ACCOUNT_SERIES else a["label"]
        out[name] = out.get(name, 0.0) + daily[a["id"]]
    out["total"] = out.sum(axis=1) if len(out.columns) else 0.0
    return out


def kpis(values: pd.DataFrame, rows: list[dict]) -> dict:
    with_cost = [r for r in rows if r["cost_basis"] is not None]
    cost = sum(r["cost_basis"] for r in with_cost)
    gain = sum(r["gain"] for r in with_cost)
    out = {"value": None, "change": None, "change_pct": None, "start": None, "end": None,
           "cost_basis": round(cost, 2) if with_cost else None, "gain": round(gain, 2) if with_cost else None,
           "gain_pct": gain / cost if cost else None, "holdings": len(rows),
           "holdings_value": round(sum(r["market_value"] for r in rows), 2)}
    if not values.empty:
        first, last = float(values["total"].iloc[0]), float(values["total"].iloc[-1])
        out.update(value=round(last, 2), change=round(last - first, 2),
                   change_pct=(last - first) / first if first else None,
                   start=values.index[0].date(), end=values.index[-1].date())
    return out


def holding_history(h: pd.DataFrame, account_id: str, holding_id: str) -> pd.DataFrame:
    """date, market_value, cost_basis, price for one holding (snapshot days only)."""
    sub = h[(h["account_id"] == account_id) & (h["holding_id"] == holding_id)].sort_values("date")
    return pd.DataFrame({"date": sub["date"], "market_value": sub["market_value"], "cost_basis": sub["cost_basis"],
                         "price": [_price(m, s) for m, s in zip(sub["market_value"], sub["shares"])]})


def load(s: Session, account_ids: list[str] | None = None):
    """(accounts, daily balances, holdings) for the selection."""
    accounts = investment_accounts(s, account_ids)
    ids = [a["id"] for a in accounts]
    daily, _ = nw.load(s, ids) if ids else (pd.DataFrame(dtype=float), None)
    return accounts, daily, load_holdings(s, ids)
