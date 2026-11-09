"""SQLAlchemy ORM models."""
from __future__ import annotations

import datetime as dt

from sqlalchemy import (
    Boolean,
    Date,
    DateTime,
    Float,
    ForeignKey,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship

ACCOUNT_TYPES = [
    "checking",
    "savings",
    "credit_card",
    "brokerage",
    "retirement",
    "loan",
    "mortgage",
    "crypto",
    "other",
]
LIABILITY_TYPES = {"credit_card", "loan", "mortgage"}
INVESTMENT_TYPES = {"brokerage", "retirement", "crypto"}

CATEGORY_GROUPS = ["income", "expense", "transfer"]


def utcnow() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc).replace(tzinfo=None)


class Base(DeclarativeBase):
    pass


class Account(Base):
    __tablename__ = "accounts"

    id: Mapped[str] = mapped_column(String, primary_key=True)  # SimpleFIN account id
    source: Mapped[str] = mapped_column(String, default="simplefin")
    org_name: Mapped[str | None] = mapped_column(String)
    org_domain: Mapped[str | None] = mapped_column(String)
    name: Mapped[str] = mapped_column(String)
    display_name: Mapped[str | None] = mapped_column(String)
    currency: Mapped[str] = mapped_column(String, default="USD")
    type: Mapped[str | None] = mapped_column(String)  # one of ACCOUNT_TYPES, set by user
    hidden: Mapped[bool] = mapped_column(Boolean, default=False)
    include_in_networth: Mapped[bool] = mapped_column(Boolean, default=True)
    linked_empower_name: Mapped[str | None] = mapped_column(String)
    balance: Mapped[float | None] = mapped_column(Float)
    available_balance: Mapped[float | None] = mapped_column(Float)
    balance_date: Mapped[dt.datetime | None] = mapped_column(DateTime)
    first_seen: Mapped[dt.datetime] = mapped_column(DateTime, default=utcnow)
    last_seen: Mapped[dt.datetime] = mapped_column(DateTime, default=utcnow)

    transactions: Mapped[list[Transaction]] = relationship(back_populates="account")

    @property
    def label(self) -> str:
        return self.display_name or self.name


class BalanceSnapshot(Base):
    __tablename__ = "balance_snapshots"

    account_id: Mapped[str] = mapped_column(ForeignKey("accounts.id"), primary_key=True)
    date: Mapped[dt.date] = mapped_column(Date, primary_key=True)
    balance: Mapped[float] = mapped_column(Float)
    available_balance: Mapped[float | None] = mapped_column(Float)


class HoldingSnapshot(Base):
    __tablename__ = "holding_snapshots"

    account_id: Mapped[str] = mapped_column(ForeignKey("accounts.id"), primary_key=True)
    holding_id: Mapped[str] = mapped_column(String, primary_key=True)
    date: Mapped[dt.date] = mapped_column(Date, primary_key=True)
    symbol: Mapped[str | None] = mapped_column(String)
    description: Mapped[str | None] = mapped_column(String)
    currency: Mapped[str | None] = mapped_column(String)
    shares: Mapped[float | None] = mapped_column(Float)
    cost_basis: Mapped[float | None] = mapped_column(Float)
    market_value: Mapped[float | None] = mapped_column(Float)
    purchase_price: Mapped[float | None] = mapped_column(Float)


class Category(Base):
    __tablename__ = "categories"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String, unique=True)
    group: Mapped[str] = mapped_column(String)  # one of CATEGORY_GROUPS
    parent: Mapped[str | None] = mapped_column(String)  # optional label for grouping in charts
    sort_order: Mapped[int | None] = mapped_column(Integer, default=0)


class Transaction(Base):
    __tablename__ = "transactions"
    __table_args__ = (UniqueConstraint("account_id", "source", "external_id"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    account_id: Mapped[str] = mapped_column(ForeignKey("accounts.id"), index=True)
    source: Mapped[str] = mapped_column(String, default="simplefin")  # simplefin | empower
    external_id: Mapped[str] = mapped_column(String)
    date: Mapped[dt.date] = mapped_column(Date, index=True)  # local posted date
    posted_at: Mapped[dt.datetime | None] = mapped_column(DateTime)  # UTC
    transacted_at: Mapped[dt.datetime | None] = mapped_column(DateTime)  # UTC
    amount: Mapped[float] = mapped_column(Float)  # negative = money out
    description: Mapped[str] = mapped_column(String, default="")
    payee: Mapped[str | None] = mapped_column(String)
    memo: Mapped[str | None] = mapped_column(String)
    pending: Mapped[bool] = mapped_column(Boolean, default=False)
    category_id: Mapped[int | None] = mapped_column(ForeignKey("categories.id"))
    category_source: Mapped[str | None] = mapped_column(String)  # rule | manual | empower
    tags: Mapped[str | None] = mapped_column(String)
    transfer_pair_id: Mapped[int | None] = mapped_column(Integer)
    note: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[dt.datetime] = mapped_column(DateTime, default=utcnow)
    updated_at: Mapped[dt.datetime] = mapped_column(DateTime, default=utcnow, onupdate=utcnow)

    account: Mapped[Account] = relationship(back_populates="transactions")
    category: Mapped[Category | None] = relationship()


class CategoryMap(Base):
    """Maps an Empower category name to an app category."""

    __tablename__ = "category_map"

    empower_category: Mapped[str] = mapped_column(String, primary_key=True)
    category_id: Mapped[int | None] = mapped_column(ForeignKey("categories.id"))


class Rule(Base):
    __tablename__ = "rules"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    field: Mapped[str] = mapped_column(String, default="description")  # description | payee | memo
    pattern: Mapped[str] = mapped_column(String)
    match_type: Mapped[str] = mapped_column(String, default="contains")  # contains | regex | equals
    category_id: Mapped[int] = mapped_column(ForeignKey("categories.id"))
    priority: Mapped[int] = mapped_column(Integer, default=100)  # lower runs first
    account_id: Mapped[str | None] = mapped_column(ForeignKey("accounts.id"))  # optional scope
    origin: Mapped[str | None] = mapped_column(String, default="user")  # user | empower (learned)


class EmpowerRow(Base):
    """Raw row from an Empower transactions CSV export (staging table).

    Rows are turned into `transactions` by finance.importers.empower.apply_empower(),
    which decides the target account (linked SimpleFIN account or a history-only
    account) and skips rows already covered by SimpleFIN.
    """

    __tablename__ = "empower_rows"

    id: Mapped[str] = mapped_column(String, primary_key=True)  # stable hash of the row
    account_name: Mapped[str] = mapped_column(String, index=True)
    date: Mapped[dt.date] = mapped_column(Date, index=True)
    description: Mapped[str] = mapped_column(String, default="")
    category: Mapped[str | None] = mapped_column(String)
    tags: Mapped[str | None] = mapped_column(String)
    amount: Mapped[float] = mapped_column(Float)  # normalized: negative = money out
    file_name: Mapped[str | None] = mapped_column(String)
    imported_at: Mapped[dt.datetime] = mapped_column(DateTime, default=utcnow)


class SyncLog(Base):
    __tablename__ = "sync_log"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    started_at: Mapped[dt.datetime] = mapped_column(DateTime, default=utcnow)
    finished_at: Mapped[dt.datetime | None] = mapped_column(DateTime)
    status: Mapped[str] = mapped_column(String, default="running")  # running | ok | partial | error
    mode: Mapped[str | None] = mapped_column(String)  # initial | incremental
    requests: Mapped[int] = mapped_column(Integer, default=0)
    accounts: Mapped[int] = mapped_column(Integer, default=0)
    transactions_new: Mapped[int] = mapped_column(Integer, default=0)
    transactions_updated: Mapped[int] = mapped_column(Integer, default=0)
    errors_json: Mapped[str | None] = mapped_column(Text)
    raw_paths: Mapped[str | None] = mapped_column(Text)
