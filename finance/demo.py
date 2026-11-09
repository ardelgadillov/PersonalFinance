"""Fake data for testing the app without touching real accounts.

A deterministic simulation of one household, day by day from ANCHOR to today:
paychecks, rent, bills, credit-card spending, savings, a car loan, a brokerage
account and a 401(k) with price random walks. The same seed always produces the
same transactions with the same ids, so repeated syncs behave like a real bank.

Used by:
- FakeSimpleFINClient: answers sync requests in SimpleFIN JSON format.
- build_demo(): creates data/demo/finance.db with a year of daily balance and
  holdings snapshots (as if the app had been syncing daily), runs a full sync,
  and writes a fake Empower CSV export to data/demo/import/.
"""
from __future__ import annotations

import calendar
import csv
import datetime as dt
import functools
import random
import shutil
from dataclasses import dataclass, field
from pathlib import Path
from zoneinfo import ZoneInfo

from sqlalchemy.orm import Session

from finance.config import Settings
from finance.models import Account, BalanceSnapshot, HoldingSnapshot
from finance.simplefin.client import SFResponse, parse_accounts_response

ANCHOR = dt.date(2024, 1, 1)
SEED = 42
SIMPLEFIN_HISTORY_DAYS = 365  # the fake "bank" only returns ~1 year of transactions
SNAPSHOT_HISTORY_DAYS = 365   # pretend the app has been syncing daily for a year


@dataclass(frozen=True)
class DemoAccount:
    id: str
    org: str
    domain: str
    name: str
    type: str
    empower_name: str
    opening: float
    in_simplefin: bool = True
    closed_on: dt.date | None = None


ACCOUNTS = [
    DemoAccount("DEMO-CHK", "Chase", "chase.com", "Total Checking", "checking",
                "Chase Total Checking - Ending in 4821", 4_200.00),
    DemoAccount("DEMO-SAV", "Ally Bank", "ally.com", "Online Savings", "savings",
                "Ally Online Savings - Ending in 7710", 12_000.00),
    DemoAccount("DEMO-CC", "Chase", "chase.com", "Sapphire Preferred", "credit_card",
                "Chase Sapphire Preferred - Ending in 3307", -850.00),
    DemoAccount("DEMO-BRK", "Fidelity Investments", "fidelity.com", "Individual - TOD", "brokerage",
                "Fidelity Individual - Ending in 5502", 0.0),
    DemoAccount("DEMO-401K", "Vanguard", "vanguard.com", "ACME Corp 401(k) Plan", "retirement",
                "Vanguard ACME Corp 401(k)", 0.0),
    DemoAccount("DEMO-AUTO", "Toyota Financial Services", "toyotafinancial.com", "Auto Loan", "loan",
                "Toyota Financial Auto Loan - Ending in 0193", -24_000.00),
    # Closed before SimpleFIN was set up: only appears in the Empower export.
    DemoAccount("DEMO-OLDCHK", "Wells Fargo", "wellsfargo.com", "Everyday Checking", "checking",
                "Wells Fargo Everyday Checking - Ending in 6614", 1_500.00,
                in_simplefin=False, closed_on=dt.date(2024, 8, 30)),
]
ACCOUNTS_BY_ID = {a.id: a for a in ACCOUNTS}

# symbol -> (description, start price, annual drift, annual volatility)
SECURITIES = {
    "VTI": ("Vanguard Total Stock Market ETF", 236.0, 0.09, 0.16),
    "VXUS": ("Vanguard Total International Stock ETF", 58.0, 0.06, 0.15),
    "BND": ("Vanguard Total Bond Market ETF", 72.5, 0.02, 0.05),
    "VFIFX": ("Vanguard Target Retirement 2050 Fund", 46.0, 0.08, 0.13),
    "SPAXX": ("Fidelity Government Money Market (cash)", 1.0, 0.0, 0.0),
}
OPENING_POSITIONS = {  # account -> symbol -> shares (cost basis = shares * start price)
    "DEMO-BRK": {"VTI": 60, "VXUS": 80, "BND": 40, "SPAXX": 500},
    "DEMO-401K": {"VFIFX": 1_150},
}
BROKERAGE_TARGET = {"VTI": 0.6, "VXUS": 0.25, "BND": 0.15}


@dataclass
class DemoTxn:
    id: str
    account_id: str
    date: dt.date
    amount: float
    description: str          # raw bank description (SimpleFIN)
    payee: str                # cleaned name (Empower "Description")
    empower_category: str
    tags: str = ""


@dataclass
class Position:
    shares: float = 0.0
    cost: float = 0.0


@dataclass
class World:
    txns: list[DemoTxn] = field(default_factory=list)
    balances: dict[str, dict[dt.date, float]] = field(default_factory=dict)
    positions: dict[str, dict[dt.date, dict[str, tuple[float, float]]]] = field(default_factory=dict)
    prices: dict[str, dict[dt.date, float]] = field(default_factory=dict)
    end: dt.date = ANCHOR


class _Sim:
    def __init__(self, end: dt.date, seed: int):
        self.rng = random.Random(seed)
        self.end = end
        self.cash = {a.id: a.opening for a in ACCOUNTS}
        self.pos: dict[str, dict[str, Position]] = {
            acct: {sym: Position(sh, round(sh * SECURITIES[sym][1], 2)) for sym, sh in syms.items()}
            for acct, syms in OPENING_POSITIONS.items()
        }
        self.price = {sym: v[1] for sym, v in SECURITIES.items()}
        self.world = World(end=end)
        self.seq: dict[tuple[str, dt.date], int] = {}
        self.salary = 3_250.00
        self.payday = dt.date(2024, 1, 5)  # every other Friday from here

    # -- helpers -----------------------------------------------------------
    def add(self, acct: str, day: dt.date, amount: float, desc: str, payee: str,
            cat: str, tags: str = "") -> None:
        n = self.seq.get((acct, day), 0) + 1
        self.seq[(acct, day)] = n
        amount = round(amount, 2)
        self.world.txns.append(DemoTxn(f"{acct}-{day:%Y%m%d}-{n:02d}", acct, day, amount,
                                       desc, payee, cat, tags))
        if acct in self.pos:  # investment accounts: cash lives in SPAXX / is invested
            if "SPAXX" in self.pos[acct]:
                p = self.pos[acct]["SPAXX"]
                p.shares += amount
                p.cost += amount
        else:
            self.cash[acct] += amount

    def chance(self, p: float) -> bool:
        return self.rng.random() < p

    def amt(self, lo: float, hi: float) -> float:
        return round(self.rng.uniform(lo, hi), 2)

    def buy(self, acct: str, day: dt.date, sym: str, dollars: float, from_cash: bool) -> None:
        price = self.price[sym]
        shares = round(dollars / price, 4)
        p = self.pos[acct].setdefault(sym, Position())
        p.shares += shares
        p.cost += round(dollars, 2)
        if from_cash:
            self.add(acct, day, -dollars, f"YOU BOUGHT {SECURITIES[sym][0].upper()} ({sym})",
                     f"Buy {sym}", "Securities Trades")

    # -- simulation ----------------------------------------------------------
    def run(self) -> World:
        day = ANCHOR
        while day <= self.end:
            self.step_prices(day)
            self.step_day(day)
            self.record(day)
            day += dt.timedelta(days=1)
        return self.world

    def step_prices(self, day: dt.date) -> None:
        if day.weekday() < 5:
            for sym, (_, _, mu, sigma) in SECURITIES.items():
                if sigma:
                    shock = self.rng.gauss(mu / 252, sigma / 252 ** 0.5)
                    self.price[sym] = round(self.price[sym] * (1 + shock), 4)
        for sym, px in self.price.items():
            self.world.prices.setdefault(sym, {})[day] = px

    def step_day(self, d: dt.date) -> None:  # noqa: C901 - a long list of life events
        last_dom = calendar.monthrange(d.year, d.month)[1]
        old_open = d <= ACCOUNTS_BY_ID["DEMO-OLDCHK"].closed_on

        if d.month == 1 and d.day == 1 and d.year > ANCHOR.year:
            self.salary = round(self.salary * 1.035, 2)  # yearly raise

        # Paycheck every other Friday + 401(k) contribution
        if d >= self.payday and (d - self.payday).days % 14 == 0:
            self.add("DEMO-CHK", d, self.salary + self.amt(-5, 5), "ACME CORP PAYROLL PPD ID: 9876543210",
                     "ACME Corp", "Paychecks/Salary")
            contrib = round(self.salary * 0.18, 2)
            self.add("DEMO-401K", d, contrib, "CONTRIBUTION EMPLOYEE PRE-TAX", "401(k) Contribution",
                     "Retirement Contributions")
            match = round(self.salary * 0.06, 2)
            self.add("DEMO-401K", d, match, "CONTRIBUTION EMPLOYER MATCH", "Employer Match",
                     "Retirement Contributions")
            self.buy("DEMO-401K", d, "VFIFX", contrib + match, from_cash=False)  # invested immediately

        # Housing & bills from checking
        if d.day == 1:
            self.add("DEMO-CHK", d, -2_150.00, "OAKWOOD APARTMENTS WEB PMTS", "Oakwood Apartments", "Rent")
        if d.day == 6:
            winter = d.month in (12, 1, 2)
            summer = d.month in (6, 7, 8)
            self.add("DEMO-CHK", d, -(self.amt(140, 190) if winter or summer else self.amt(75, 110)),
                     "CON EDISON EBILL PAYMENT", "Con Edison", "Utilities")
        if d.day == 9:
            self.add("DEMO-CHK", d, -24.99, "PLANET FITNESS CLUB FEES", "Planet Fitness", "Personal Care")
        if d.day == 15:
            payment = 465.00
            interest = round(-self.cash["DEMO-AUTO"] * 0.049 / 12, 2)
            self.add("DEMO-CHK", d, -payment, "TOYOTA ACH LEASE/LOAN PMT", "Toyota Financial Services", "Loans")
            self.add("DEMO-AUTO", d, -interest, "INTEREST CHARGE", "Interest Charge", "Interest")
            self.add("DEMO-AUTO", d, payment, "PAYMENT RECEIVED - THANK YOU", "Payment", "Loans")
        if d.day == 20:
            self.add("DEMO-CHK", d, -500.00, "ONLINE TRANSFER TO ALLY BANK SAVINGS", "Transfer to Ally", "Transfers")
            self.add("DEMO-SAV", d, 500.00, "ONLINE TRANSFER FROM CHASE CHECKING", "Transfer from Chase", "Transfers")
        if d.day == 22:
            self.add("DEMO-CHK", d, -1_000.00, "FID BKG SVC LLC MONEYLINE", "Fidelity Investments", "Transfers")
            self.add("DEMO-BRK", d, 1_000.00, "ELECTRONIC FUNDS TRANSFER RECEIVED", "Transfer from Chase",
                     "Transfers")
            for sym, w in BROKERAGE_TARGET.items():
                self.buy("DEMO-BRK", d, sym, round(1_000 * w, 2), from_cash=True)
        if d.day == last_dom:
            interest = round(self.cash["DEMO-SAV"] * 0.0400 / 12, 2)
            self.add("DEMO-SAV", d, interest, "INTEREST PAID", "Ally Bank", "Interest")
        if d.month in (3, 6, 9, 12) and d.day == 26:
            for sym, yld in (("VTI", 0.013), ("VXUS", 0.03), ("BND", 0.035)):
                p = self.pos["DEMO-BRK"].get(sym)
                if p and p.shares:
                    div = round(p.shares * self.price[sym] * yld / 4, 2)
                    self.add("DEMO-BRK", d, div, f"DIVIDEND RECEIVED {SECURITIES[sym][0].upper()} ({sym})",
                             f"{sym} Dividend", "Dividends Received")
        if d.month == 4 and d.day == 12:
            self.add("DEMO-CHK", d, 1_180.00 + self.amt(-200, 200), "IRS TREAS 310 TAX REF", "IRS",
                     "Other Income")
        if d.month in (2, 8) and d.day == 3:
            self.add("DEMO-CHK", d, -642.00, "GEICO *AUTO INSURANCE", "GEICO", "Insurance")
        if self.chance(0.02):
            self.add("DEMO-CHK", d, self.amt(20, 150), "VENMO CASHOUT", "Venmo", "Other Income")
        if self.chance(0.03):
            self.add("DEMO-CHK", d, -self.amt(40, 200), "ATM WITHDRAWAL 0042 BROADWAY", "ATM Withdrawal",
                     "ATM/Cash")

        # Credit card spending
        cc = "DEMO-CC"
        if self.chance(2.2 / 7):
            store = self.rng.choice([("WHOLEFDS MKT #10234", "Whole Foods"),
                                     ("TRADER JOE S #552", "Trader Joe's"),
                                     ("KROGER #0731", "Kroger")])
            self.add(cc, d, -self.amt(35, 185), *store, "Groceries")
        if self.chance(3 / 7):
            place = self.rng.choice([("CHIPOTLE 2291", "Chipotle", 11, 28),
                                     ("STARBUCKS STORE 08812", "Starbucks", 4, 14),
                                     ("SQ *BLUE BOTTLE COFFEE", "Blue Bottle Coffee", 5, 16),
                                     ("TST* LUIGIS TRATTORIA", "Luigi's Trattoria", 45, 140),
                                     ("DOORDASH*THAI GARDEN", "DoorDash", 22, 60)])
            self.add(cc, d, -self.amt(place[2], place[3]), place[0], place[1], "Restaurants")
        if d.weekday() == 5 and self.chance(0.8):
            self.add(cc, d, -self.amt(35, 62), "SHELL OIL 57444", "Shell", "Gasoline/Fuel")
        if self.chance(1 / 7):
            shop = self.rng.choice([("AMAZON.COM*2K7LM9", "Amazon"),
                                    ("TARGET 00012345", "Target"),
                                    ("COSTCO WHSE #0412", "Costco")])
            self.add(cc, d, -self.amt(15, 220), *shop, "General Merchandise")
        if self.chance(0.015):
            self.add(cc, d, self.amt(15, 80), "AMAZON.COM REFUND", "Amazon", "Refunds & Reimbursements")
        if self.chance(0.04):
            self.add(cc, d, -self.amt(12, 95), "CVS/PHARMACY #04471", "CVS Pharmacy", "Healthcare/Medical")
        if self.chance(0.05):
            ent = self.rng.choice([("AMC THEATRES 0612", "AMC Theatres"), ("TICKETMASTER", "Ticketmaster"),
                                   ("STEAMGAMES.COM", "Steam")])
            self.add(cc, d, -self.amt(15, 120), *ent, "Entertainment")
        for dom, desc, payee, price, cat in (
            (8, "NETFLIX.COM", "Netflix", 15.49, "Online Services"),
            (12, "SPOTIFY USA", "Spotify", 11.99, "Online Services"),
            (17, "AMAZON PRIME*MEMBERSHIP", "Amazon Prime", 14.99, "Online Services"),
            (11, "VERIZON WIRELESS PAYMENTS", "Verizon", 85.00, "Telephone"),
            (14, "COMCAST XFINITY", "Xfinity", 70.00, "Cable/Satellite"),
        ):
            if d.day == dom:
                self.add(cc, d, -price, desc, payee, cat)
        # Two trips a year
        if (d.month, d.day) in ((3, 2), (7, 10)):
            tag = f"Vacation {d.year}"
            self.add(cc, d, -self.amt(380, 620), "DELTA AIR LINES 0062", "Delta Air Lines", "Travel", tag)
            self.add(cc, d, -self.amt(500, 900), "MARRIOTT HOTELS", "Marriott",
                     "Travel", tag)
        # Pay the card in full on the 25th
        if d.day == 25 and self.cash[cc] < 0:
            due = round(-self.cash[cc], 2)
            self.add("DEMO-CHK", d, -due, "CHASE CREDIT CRD AUTOPAY PPD", "Chase Credit Card",
                     "Credit Card Payments")
            self.add(cc, d, due, "AUTOMATIC PAYMENT - THANK YOU", "Payment", "Credit Card Payments")

        # Old Wells Fargo account until it is closed
        if old_open:
            if d.day == 3:
                self.add("DEMO-OLDCHK", d, -12.00, "MONTHLY SERVICE FEE", "Wells Fargo", "Service Charges/Fees")
            if d == ACCOUNTS_BY_ID["DEMO-OLDCHK"].closed_on:
                bal = round(self.cash["DEMO-OLDCHK"], 2)
                self.add("DEMO-OLDCHK", d, -bal, "ONLINE TRANSFER TO CHASE CHECKING", "Transfer to Chase",
                         "Transfers")
                self.add("DEMO-CHK", d, bal, "ONLINE TRANSFER FROM WELLS FARGO", "Transfer from Wells Fargo",
                         "Transfers")

    def record(self, d: dt.date) -> None:
        for a in ACCOUNTS:
            if a.id in self.pos:
                holdings = {s: (round(p.shares, 4), round(p.cost, 2))
                            for s, p in self.pos[a.id].items() if abs(p.shares) > 1e-9}
                self.world.positions.setdefault(a.id, {})[d] = holdings
                bal = sum(sh * self.price[s] for s, (sh, _) in holdings.items())
            else:
                bal = self.cash[a.id]
            self.world.balances.setdefault(a.id, {})[d] = round(bal, 2)


@functools.lru_cache(maxsize=4)
def simulate(end: dt.date, seed: int = SEED) -> World:
    return _Sim(end, seed).run()


def _today(tz: str) -> dt.date:
    return dt.datetime.now(ZoneInfo(tz)).date()


def _epoch(day: dt.date, tz: ZoneInfo, hour: int = 12) -> int:
    return int(dt.datetime.combine(day, dt.time(hour), tz).timestamp())


def holdings_on(world: World, acct: str, day: dt.date) -> list[dict]:
    out = []
    for sym, (shares, cost) in world.positions.get(acct, {}).get(day, {}).items():
        price = world.prices[sym][day]
        out.append({
            "id": f"{acct}-{sym}",
            "symbol": sym,
            "description": SECURITIES[sym][0],
            "currency": "USD",
            "shares": f"{shares:.4f}",
            "cost_basis": f"{cost:.2f}",
            "market_value": f"{shares * price:.2f}",
            "purchase_price": f"{(cost / shares) if shares else 0:.4f}",
            "created": None,
        })
    return out


class FakeSimpleFINClient:
    """Drop-in replacement for SimpleFINClient that serves simulated data."""

    def __init__(self, timezone: str = "America/New_York", today: dt.date | None = None):
        self.tz = ZoneInfo(timezone)
        self.today = today or _today(timezone)

    def get_accounts(self, start: dt.datetime | None = None, end: dt.datetime | None = None,
                     pending: bool = True, balances_only: bool = False) -> SFResponse:
        world = simulate(self.today)

        def local(ts: dt.datetime | None, default: dt.date) -> dt.date:
            if ts is None:
                return default
            return ts.replace(tzinfo=dt.timezone.utc).astimezone(self.tz).date()

        oldest = self.today - dt.timedelta(days=SIMPLEFIN_HISTORY_DAYS)
        lo = max(local(start, oldest), oldest)
        hi = local(end, self.today)

        accounts = []
        for a in ACCOUNTS:
            if not a.in_simplefin:
                continue
            txns = []
            if not balances_only:
                for t in world.txns:
                    if t.account_id == a.id and lo <= t.date <= hi:
                        is_pending = t.date >= self.today - dt.timedelta(days=1) and a.id == "DEMO-CC"
                        if is_pending and not pending:
                            continue
                        txns.append({
                            "id": t.id,
                            "posted": 0 if is_pending else _epoch(t.date, self.tz),
                            "amount": f"{t.amount:.2f}",
                            "description": t.description,
                            "payee": t.payee,
                            "memo": "",
                            "transacted_at": _epoch(t.date, self.tz, 10),
                            "pending": is_pending,
                        })
            accounts.append({
                "org": {"name": a.org, "domain": a.domain, "sfin-url": "https://demo.invalid/"},
                "id": a.id,
                "name": a.name,
                "currency": "USD",
                "balance": f"{world.balances[a.id][self.today]:.2f}",
                "available-balance": f"{world.balances[a.id][self.today]:.2f}",
                "balance-date": _epoch(self.today, self.tz, 8),
                "transactions": txns,
                "holdings": holdings_on(world, a.id, self.today),
            })
        return parse_accounts_response({"errors": [], "accounts": accounts})


def write_empower_csv(path: Path, world: World, until: dt.date | None = None) -> int:
    """Fake Empower export: Date, Account, Description, Category, Tags, Amount."""
    path.parent.mkdir(parents=True, exist_ok=True)
    rows = [t for t in world.txns if until is None or t.date <= until]
    rows.sort(key=lambda t: (t.date, t.account_id, t.id), reverse=True)  # Empower lists newest first
    with path.open("w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["Date", "Account", "Description", "Category", "Tags", "Amount"])
        for t in rows:
            w.writerow([t.date.isoformat(), ACCOUNTS_BY_ID[t.account_id].empower_name, t.payee,
                        t.empower_category, t.tags, f"{t.amount:.2f}"])
    return len(rows)


def build_demo(settings: Settings, reset: bool = False, today: dt.date | None = None) -> dict:
    """Create the demo database: sync from the fake bank + a year of snapshots + Empower CSV."""
    from finance.db import get_engine, init_db
    from finance.sync import run_sync

    if not settings.demo:
        raise RuntimeError("build_demo() only runs with demo settings (data/demo)")
    if settings.db_path.exists():
        if not reset:
            raise FileExistsError(f"{settings.db_path} already exists (use --reset to rebuild it)")
        from finance import db as dbmod
        for eng in dbmod._engines.values():
            eng.dispose()
        dbmod._engines.clear()
        shutil.rmtree(settings.data_dir)

    today = today or _today(settings.timezone)
    settings.data_dir.mkdir(parents=True, exist_ok=True)
    engine = init_db(get_engine(settings.db_url))
    client = FakeSimpleFINClient(settings.timezone, today=today)
    result = run_sync(client=client, engine=engine, settings=settings, full=True, max_requests=12)

    world = simulate(today)
    with Session(engine) as s:
        for a in ACCOUNTS:
            if not a.in_simplefin:
                continue
            acct = s.get(Account, a.id)
            acct.type = a.type
            acct.linked_empower_name = None  # linking is done in the app (Milestone 2)
            for back in range(1, SNAPSHOT_HISTORY_DAYS + 1):
                day = today - dt.timedelta(days=back)
                s.merge(BalanceSnapshot(account_id=a.id, date=day, balance=world.balances[a.id][day],
                                        available_balance=world.balances[a.id][day]))
                for h in holdings_on(world, a.id, day):
                    s.merge(HoldingSnapshot(
                        account_id=a.id, holding_id=h["id"], date=day, symbol=h["symbol"] or None,
                        description=h["description"], currency="USD", shares=float(h["shares"]),
                        cost_basis=float(h["cost_basis"]), market_value=float(h["market_value"]),
                        purchase_price=float(h["purchase_price"]),
                    ))
        s.commit()

    csv_path = settings.import_dir / "empower_transactions.csv"
    n_csv = write_empower_csv(csv_path, world)
    return {"sync": result, "csv_path": csv_path, "csv_rows": n_csv, "db_path": settings.db_path}
