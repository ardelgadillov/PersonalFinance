"""Minimal SimpleFIN Bridge client.

Protocol: https://www.simplefin.org/protocol.html
The access URL embeds Basic-auth credentials: https://user:pass@host/simplefin
"""
from __future__ import annotations

import datetime as dt
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urlsplit, urlunsplit, unquote

import requests


class SimpleFINError(RuntimeError):
    pass


def _num(value: Any) -> float | None:
    if value is None or value == "":
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _ts(value: Any) -> dt.datetime | None:
    """Epoch seconds -> naive UTC datetime (None for missing/0)."""
    if value in (None, "", 0, "0"):
        return None
    try:
        return dt.datetime.fromtimestamp(int(value), tz=dt.timezone.utc).replace(tzinfo=None)
    except (TypeError, ValueError, OSError):
        return None


@dataclass
class SFTransaction:
    id: str
    posted: dt.datetime | None
    amount: float
    description: str
    payee: str | None
    memo: str | None
    transacted_at: dt.datetime | None
    pending: bool


@dataclass
class SFHolding:
    id: str
    symbol: str | None
    description: str | None
    currency: str | None
    shares: float | None
    cost_basis: float | None
    market_value: float | None
    purchase_price: float | None
    created: dt.datetime | None


@dataclass
class SFAccount:
    id: str
    name: str
    currency: str
    balance: float | None
    available_balance: float | None
    balance_date: dt.datetime | None
    org_name: str | None
    org_domain: str | None
    transactions: list[SFTransaction] = field(default_factory=list)
    holdings: list[SFHolding] = field(default_factory=list)


@dataclass
class SFResponse:
    accounts: list[SFAccount]
    errors: list[str]
    raw: dict


def parse_accounts_response(data: dict) -> SFResponse:
    # Protocol v2 may list institutions under "connections" referenced by conn_id.
    connections = {c.get("conn_id") or c.get("id"): c for c in data.get("connections", []) or []}

    accounts: list[SFAccount] = []
    for a in data.get("accounts", []) or []:
        org = a.get("org") or connections.get(a.get("conn_id")) or {}
        txns = [
            SFTransaction(
                id=str(t["id"]),
                posted=_ts(t.get("posted")),
                amount=_num(t.get("amount")) or 0.0,
                description=(t.get("description") or "").strip(),
                payee=(t.get("payee") or None),
                memo=(t.get("memo") or None),
                transacted_at=_ts(t.get("transacted_at")),
                pending=bool(t.get("pending")) or not t.get("posted"),
            )
            for t in a.get("transactions", []) or []
        ]
        holdings = [
            SFHolding(
                id=str(h.get("id") or h.get("symbol") or h.get("description")),
                symbol=h.get("symbol") or None,
                description=h.get("description") or None,
                currency=h.get("currency") or None,
                shares=_num(h.get("shares")),
                cost_basis=_num(h.get("cost_basis")),
                market_value=_num(h.get("market_value")),
                purchase_price=_num(h.get("purchase_price")),
                created=_ts(h.get("created")),
            )
            for h in a.get("holdings", []) or []
        ]
        accounts.append(
            SFAccount(
                id=str(a["id"]),
                name=a.get("name") or str(a["id"]),
                currency=a.get("currency") or "USD",
                balance=_num(a.get("balance")),
                available_balance=_num(a.get("available-balance")),
                balance_date=_ts(a.get("balance-date")),
                org_name=org.get("name"),
                org_domain=org.get("domain"),
                transactions=txns,
                holdings=holdings,
            )
        )

    errors = [str(e.get("msg", e)) if isinstance(e, dict) else str(e)
              for e in (data.get("errors") or []) + (data.get("errlist") or [])]
    return SFResponse(accounts=accounts, errors=errors, raw=data)


class SimpleFINClient:
    def __init__(self, access_url: str, timeout: float = 60.0):
        if not access_url:
            raise SimpleFINError("SIMPLEFIN_ACCESS_URL is not set (add it to .env)")
        parts = urlsplit(access_url.strip())
        if not parts.username:
            raise SimpleFINError("Access URL must contain credentials (https://user:pass@host/...)")
        self._auth = (unquote(parts.username), unquote(parts.password or ""))
        netloc = parts.hostname + (f":{parts.port}" if parts.port else "")
        self._base = urlunsplit((parts.scheme, netloc, parts.path.rstrip("/"), "", ""))
        self._timeout = timeout

    def get_accounts(
        self,
        start: dt.datetime | None = None,
        end: dt.datetime | None = None,
        pending: bool = True,
        balances_only: bool = False,
    ) -> SFResponse:
        params: dict[str, str] = {}
        if start:
            params["start-date"] = str(int(start.replace(tzinfo=dt.timezone.utc).timestamp()))
        if end:
            params["end-date"] = str(int(end.replace(tzinfo=dt.timezone.utc).timestamp()))
        if pending:
            params["pending"] = "1"
        if balances_only:
            params["balances-only"] = "1"

        try:
            resp = requests.get(
                f"{self._base}/accounts", params=params, auth=self._auth, timeout=self._timeout
            )
        except requests.RequestException as exc:
            raise SimpleFINError(f"Request to SimpleFIN failed: {exc}") from exc
        if resp.status_code == 403:
            raise SimpleFINError("SimpleFIN returned 403: access revoked or credentials invalid")
        if resp.status_code == 402:
            raise SimpleFINError("SimpleFIN returned 402: subscription payment required")
        if not resp.ok:
            raise SimpleFINError(f"SimpleFIN returned HTTP {resp.status_code}: {resp.text[:200]}")
        return parse_accounts_response(resp.json())
