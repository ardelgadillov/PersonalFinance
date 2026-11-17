"""Net worth analytics from daily balance snapshots (pandas, no UI).

Rules:
- History comes only from `balance_snapshots` (SimpleFIN gives current balances; one snapshot per sync day).
- Accounts count when `include_in_networth` is set and they are not hidden, unless explicitly selected.
- A balance is carried forward over days without a snapshot (missed syncs), up to the last day the account
  was seen by SimpleFIN, so closed / disconnected accounts drop out instead of being frozen forever.
- Liability accounts (credit card, loan, mortgage) always count as debt: -abs(balance), whatever sign the
  bank reports. Accounts without a type are an asset or a debt by the sign of their latest balance.
"""
from __future__ import annotations

import datetime as dt

import pandas as pd
from sqlalchemy import select
from sqlalchemy.orm import Session

from finance.models import LIABILITY_TYPES, Account, BalanceSnapshot

# (key, label, is_liability) in stacking order; colors live in charts.THEMES under "nw_<key>".
GROUPS = [
    ("cash", "Cash", False),
    ("investments", "Investments", False),
    ("other", "Other assets", False),
    ("credit", "Credit cards", True),
    ("loans", "Loans and other debt", True),
]
GROUP_LABEL = {k: label for k, label, _ in GROUPS}
LIABILITY_GROUPS = {k for k, _, liab in GROUPS if liab}
TYPE_GROUP = {
    "checking": "cash", "savings": "cash",
    "brokerage": "investments", "retirement": "investments", "crypto": "investments",
    "other": "other",
    "credit_card": "credit",
    "loan": "loans", "mortgage": "loans",
}
ACCOUNT_COLUMNS = ["id", "label", "institution", "type", "group", "balance_date"]


def account_group(type_: str | None, balance: float | None) -> str:
    if type_ in TYPE_GROUP:
        return TYPE_GROUP[type_]
    return "loans" if (balance or 0) < 0 else "other"


def signed(type_: str | None, balance: float) -> float:
    return -abs(balance) if type_ in LIABILITY_TYPES else balance


def load(s: Session, account_ids: list[str] | None = None) -> tuple[pd.DataFrame, pd.DataFrame]:
    """(daily, accounts).

    daily: index = every day from the first to the last snapshot, one column per account id with its
    signed balance (0 before the account's first snapshot and after it stopped being seen).
    accounts: one row per account in `daily` (ACCOUNT_COLUMNS).
    """
    q = select(Account)
    if account_ids:
        q = q.where(Account.id.in_(account_ids))
    else:
        q = q.where(Account.include_in_networth.is_(True), Account.hidden.is_(False))
    accts = {a.id: a for a in s.scalars(q)}
    snaps = s.execute(select(BalanceSnapshot.account_id, BalanceSnapshot.date, BalanceSnapshot.balance)
                      .where(BalanceSnapshot.account_id.in_(list(accts)))).all() if accts else []
    if not snaps:
        return pd.DataFrame(dtype=float), pd.DataFrame(columns=ACCOUNT_COLUMNS)
    raw = pd.DataFrame(snaps, columns=["account_id", "date", "balance"])
    raw["date"] = pd.to_datetime(raw["date"])
    wide = raw.pivot_table(index="date", columns="account_id", values="balance", aggfunc="last")
    days = pd.date_range(wide.index.min(), wide.index.max(), freq="D")
    wide = wide.reindex(days)
    rows = []
    for acct_id in wide.columns:
        a = accts[acct_id]
        col = wide[acct_id]
        last_snapshot = col.last_valid_index()
        # Manual accounts never disconnect: carry their last balance forward to the end of the range.
        if a.source == "manual":
            seen = days[-1]
        else:
            seen = pd.Timestamp(a.last_seen.date()) if a.last_seen else last_snapshot
        filled = col.ffill()
        filled[filled.index > max(last_snapshot, seen)] = float("nan")
        latest = float(col.loc[last_snapshot])
        wide[acct_id] = filled.fillna(0.0).map(lambda b, t=a.type: signed(t, b))
        rows.append({"id": acct_id, "label": a.label, "institution": a.org_name or "", "type": a.type or "",
                     "group": account_group(a.type, latest), "balance_date": last_snapshot.date()})
    wide.index.name = "date"
    return wide, pd.DataFrame(rows, columns=ACCOUNT_COLUMNS)


def clip(daily: pd.DataFrame, start: dt.date | None, end: dt.date | None) -> pd.DataFrame:
    if daily.empty:
        return daily
    lo = pd.Timestamp(start) if start else daily.index.min()
    hi = pd.Timestamp(end) if end else daily.index.max()
    return daily[(daily.index >= lo) & (daily.index <= hi)]


def by_group(daily: pd.DataFrame, accounts: pd.DataFrame) -> pd.DataFrame:
    """index date; one column per group present (GROUPS order) + assets, liabilities, net_worth."""
    out = pd.DataFrame(index=daily.index)
    for key, _, _ in GROUPS:
        ids = list(accounts.loc[accounts["group"] == key, "id"]) if not accounts.empty else []
        if ids:
            out[key] = daily[ids].sum(axis=1)
    asset_cols = [k for k in out.columns if k not in LIABILITY_GROUPS]
    liab_cols = [k for k in out.columns if k in LIABILITY_GROUPS]
    out["assets"] = out[asset_cols].sum(axis=1) if asset_cols else 0.0
    out["liabilities"] = out[liab_cols].sum(axis=1) if liab_cols else 0.0
    out["net_worth"] = out["assets"] + out["liabilities"]
    return out


def kpis(groups: pd.DataFrame) -> dict:
    if groups.empty:
        return {"net_worth": None, "assets": None, "liabilities": None, "change": None, "change_pct": None,
                "start": None, "end": None}
    first, last = groups.iloc[0], groups.iloc[-1]
    change = float(last["net_worth"] - first["net_worth"])
    return {
        "net_worth": round(float(last["net_worth"]), 2),
        "assets": round(float(last["assets"]), 2),
        "liabilities": round(float(last["liabilities"]), 2),
        "change": round(change, 2),
        "change_pct": change / abs(first["net_worth"]) if first["net_worth"] else None,
        "start": groups.index[0].date(),
        "end": groups.index[-1].date(),
    }


def monthly_change(groups: pd.DataFrame) -> pd.DataFrame:
    """month, net_worth (at month end or last day), change vs the previous month end (first month: vs its
    first day in range)."""
    if groups.empty:
        return pd.DataFrame(columns=["month", "net_worth", "change"])
    nw = groups["net_worth"]
    month_end = nw.groupby(nw.index.to_period("M")).last()
    prev = month_end.shift(1)
    prev.iloc[0] = nw.iloc[0]
    out = pd.DataFrame({"month": month_end.index.to_timestamp(), "net_worth": month_end.values,
                        "change": (month_end - prev).values})
    return out.reset_index(drop=True)


def account_rows(daily: pd.DataFrame, accounts: pd.DataFrame) -> list[dict]:
    """Balance at the end of the range and change over it, per account (largest absolute balance first)."""
    if daily.empty:
        return []
    order = {k: i for i, (k, _, _) in enumerate(GROUPS)}
    rows = []
    for a in accounts.itertuples():
        col = daily[a.id]
        rows.append({
            "id": a.id, "account": a.label, "institution": a.institution, "type": a.type,
            "group": GROUP_LABEL[a.group], "group_key": a.group,
            "balance": round(float(col.iloc[-1]), 2), "change": round(float(col.iloc[-1] - col.iloc[0]), 2),
            "updated": a.balance_date.isoformat(), "_order": order[a.group],
        })
    rows.sort(key=lambda r: (r["_order"], -abs(r["balance"])))
    for r in rows:
        del r["_order"]
    return rows
