"""Cash flow analytics (pandas, no UI).

Rules:
- Transfers are excluded: transfer-group categories and confirmed transfer pairs.
- Hidden accounts are excluded unless explicitly selected.
- Income = income-group amounts; spending = -(expense-group amounts), so refunds inside an expense
  category reduce spending. Uncategorized transactions count as income (> 0) or spending (< 0).
"""
from __future__ import annotations

import datetime as dt

import pandas as pd
from sqlalchemy import select
from sqlalchemy.orm import Session

from finance.categorize import description_key
from finance.models import Account, Category, Transaction

UNCATEGORIZED = "Uncategorized"
COLUMNS = ["date", "month", "amount", "category", "parent", "kind", "description", "account_id"]


def load_flows(s: Session, start: dt.date | None = None, end: dt.date | None = None,
               account_ids: list[str] | None = None) -> pd.DataFrame:
    """One row per income/spending transaction in range, with kind = income | expense."""
    cats = {c.id: c for c in s.scalars(select(Category))}
    q = select(Transaction.date, Transaction.amount, Transaction.category_id, Transaction.description,
               Transaction.account_id).where(Transaction.transfer_pair_id.is_(None))
    if start:
        q = q.where(Transaction.date >= start)
    if end:
        q = q.where(Transaction.date <= end)
    if account_ids:
        q = q.where(Transaction.account_id.in_(account_ids))
    else:
        hidden = list(s.scalars(select(Account.id).where(Account.hidden.is_(True))))
        if hidden:
            q = q.where(Transaction.account_id.not_in(hidden))
    rows = []
    for d, amount, cid, desc, acct in s.execute(q):
        c = cats.get(cid)
        if c is not None and c.group == "transfer":
            continue
        kind = c.group if c is not None else ("income" if amount > 0 else "expense")
        rows.append((d, amount, c.name if c else UNCATEGORIZED, (c.parent if c else None) or "", kind,
                     desc or "", acct))
    df = pd.DataFrame(rows, columns=[c for c in COLUMNS if c != "month"])
    if df.empty:
        return pd.DataFrame(columns=COLUMNS)
    df["date"] = pd.to_datetime(df["date"])
    df["month"] = df["date"].dt.to_period("M").dt.to_timestamp()
    return df[COLUMNS]


def month_range(start: dt.date | None, end: dt.date | None, df: pd.DataFrame) -> pd.DatetimeIndex:
    """Every month in the period (so empty months still show), bounded by the data for open ranges."""
    if df.empty and not (start and end):
        return pd.DatetimeIndex([])
    lo = pd.Timestamp(start) if start else df["date"].min()
    hi = pd.Timestamp(end) if end else df["date"].max()
    return pd.date_range(lo.to_period("M").to_timestamp(), hi.to_period("M").to_timestamp(), freq="MS")


def monthly(df: pd.DataFrame, months: pd.DatetimeIndex) -> pd.DataFrame:
    """month, income, spending, net, savings_rate (None when there is no income)."""
    if df.empty:
        out = pd.DataFrame({"month": months, "income": 0.0, "spending": 0.0})
    else:
        inc = df[df["kind"] == "income"].groupby("month")["amount"].sum()
        exp = -df[df["kind"] == "expense"].groupby("month")["amount"].sum()
        out = pd.DataFrame({"income": inc, "spending": exp}).reindex(months, fill_value=0.0).fillna(0.0)
        out = out.rename_axis("month").reset_index()
    out["net"] = out["income"] - out["spending"]
    out["savings_rate"] = [n / i if i > 0 else None for n, i in zip(out["net"], out["income"])]
    return out


def kpis(df: pd.DataFrame, months: pd.DatetimeIndex) -> dict:
    income = float(df.loc[df["kind"] == "income", "amount"].sum()) if not df.empty else 0.0
    spending = float(-df.loc[df["kind"] == "expense", "amount"].sum()) if not df.empty else 0.0
    n = max(len(months), 1)
    return {
        "income": round(income, 2),
        "spending": round(spending, 2),
        "net": round(income - spending, 2),
        "savings_rate": (income - spending) / income if income > 0 else None,
        "avg_spending": round(spending / n, 2),
        "months": len(months),
    }


def by_category(df: pd.DataFrame, kind: str = "expense", months: int = 1) -> pd.DataFrame:
    """category, parent, total (positive), transactions, share, monthly_avg; largest first."""
    sub = df[df["kind"] == kind] if not df.empty else df
    if sub.empty:
        return pd.DataFrame(columns=["category", "parent", "total", "transactions", "share", "monthly_avg"])
    sign = -1 if kind == "expense" else 1
    g = sub.groupby(["category", "parent"]).agg(total=("amount", "sum"), transactions=("amount", "size"))
    g["total"] = g["total"] * sign
    g = g.reset_index().sort_values("total", ascending=False)
    positive = g.loc[g["total"] > 0, "total"].sum()
    g["share"] = g["total"] / positive if positive else 0.0
    g["monthly_avg"] = g["total"] / max(months, 1)
    return g.reset_index(drop=True)


def category_trend(df: pd.DataFrame, category: str, months: pd.DatetimeIndex) -> pd.DataFrame:
    """month, amount (spending positive for expense categories, income positive for income)."""
    sub = df[df["category"] == category] if not df.empty else df
    if sub.empty:
        return pd.DataFrame({"month": months, "amount": 0.0})
    sign = -1 if (sub["kind"] == "expense").any() else 1
    s = (sub.groupby("month")["amount"].sum() * sign).reindex(months, fill_value=0.0)
    return s.rename_axis("month").rename("amount").reset_index()


def top_merchants(df: pd.DataFrame, n: int = 50) -> list[dict]:
    """Spending grouped by merchant key (normalized description)."""
    sub = df[df["kind"] == "expense"] if not df.empty else df
    if sub.empty:
        return []
    sub = sub.assign(merchant=[description_key(d) or d.strip().upper() or "(no description)"
                               for d in sub["description"]])
    g = sub.groupby("merchant").agg(total=("amount", "sum"), transactions=("amount", "size"),
                                    last=("date", "max"),
                                    category=("category", lambda c: c.value_counts().index[0]),
                                    example=("description", "last"))
    g["total"] = -g["total"]
    g = g[g["total"] > 0].sort_values("total", ascending=False).head(n).reset_index()
    return [{"merchant": r.merchant, "total": round(r.total, 2), "transactions": int(r.transactions),
             "average": round(r.total / r.transactions, 2), "last": r.last.date().isoformat(),
             "category": r.category, "example": r.example} for r in g.itertuples()]


def sankey(df: pd.DataFrame, max_expense_nodes: int = 12) -> dict:
    """Nodes/links for income sources -> Income -> spending categories (+ Savings or From savings)."""
    income = by_category(df, "income")
    income = income[income["total"] > 0]
    spend = by_category(df, "expense")
    spend = spend[spend["total"] > 0]
    if spend.empty and income.empty:
        return {"labels": [], "kinds": [], "source": [], "target": [], "value": []}
    if len(spend) > max_expense_nodes:
        head = spend.head(max_expense_nodes - 1)
        rest = spend.iloc[max_expense_nodes - 1:]
        spend = pd.concat([head, pd.DataFrame([{"category": f"Other ({len(rest)})", "total": rest["total"].sum()}])])
    labels, kinds, src, tgt, val = ["Income"], ["hub"], [], [], []
    total_in, total_out = float(income["total"].sum()), float(spend["total"].sum())
    for r in income.itertuples():
        labels.append(r.category), kinds.append("income")
        src.append(len(labels) - 1), tgt.append(0), val.append(round(r.total, 2))
    if total_out > total_in:
        labels.append("From savings"), kinds.append("deficit")
        src.append(len(labels) - 1), tgt.append(0), val.append(round(total_out - total_in, 2))
    for r in spend.itertuples():
        labels.append(r.category), kinds.append("expense")
        src.append(0), tgt.append(len(labels) - 1), val.append(round(r.total, 2))
    if total_in > total_out:
        labels.append("Savings"), kinds.append("savings")
        src.append(0), tgt.append(len(labels) - 1), val.append(round(total_in - total_out, 2))
    return {"labels": labels, "kinds": kinds, "source": src, "target": tgt, "value": val}


# --------------------------------------------------------------------------- daily (This month view)
def month_bounds(day: dt.date) -> tuple[dt.date, dt.date]:
    first = day.replace(day=1)
    nxt = (first + dt.timedelta(days=32)).replace(day=1)
    return first, nxt - dt.timedelta(days=1)


def previous_month(day: dt.date) -> tuple[dt.date, dt.date]:
    return month_bounds(day.replace(day=1) - dt.timedelta(days=1))


def daily(df: pd.DataFrame, start: dt.date, end: dt.date) -> pd.DataFrame:
    """day, day_of_month, income, spending, net, cum_net for every day from start to end."""
    days = pd.date_range(pd.Timestamp(start), pd.Timestamp(end), freq="D")
    if df.empty:
        out = pd.DataFrame({"day": days, "income": 0.0, "spending": 0.0})
    else:
        inc = df[df["kind"] == "income"].groupby("date")["amount"].sum()
        exp = -df[df["kind"] == "expense"].groupby("date")["amount"].sum()
        out = (pd.DataFrame({"income": inc, "spending": exp}).reindex(days, fill_value=0.0).fillna(0.0)
               .rename_axis("day").reset_index())
    out["day_of_month"] = out["day"].dt.day
    out["net"] = out["income"] - out["spending"]
    out["cum_net"] = out["net"].cumsum()
    return out


def category_daily(df: pd.DataFrame, category: str, start: dt.date, end: dt.date) -> pd.DataFrame:
    """Like category_trend but per day; column `month` holds the day (same shape for the chart)."""
    days = pd.date_range(pd.Timestamp(start), pd.Timestamp(end), freq="D")
    sub = df[df["category"] == category] if not df.empty else df
    if sub.empty:
        return pd.DataFrame({"month": days, "amount": 0.0})
    sign = -1 if (sub["kind"] == "expense").any() else 1
    s = (sub.groupby("date")["amount"].sum() * sign).reindex(days, fill_value=0.0)
    return s.rename_axis("month").rename("amount").reset_index()
