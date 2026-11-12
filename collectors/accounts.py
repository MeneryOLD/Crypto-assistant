"""Users' own exchange accounts, read through their read-only API keys (ccxt).

Nothing here can place orders or move funds: only balances, prices and the key's own permissions are read.
A key that is allowed to withdraw is refused outright.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

import ccxt.async_support as ccxt

from collectors.exchanges import REQUEST_TIMEOUT, pick_symbol

log = logging.getLogger(__name__)

# ccxt id -> (display name, needs a passphrase, where to create the key)
SUPPORTED: dict[str, tuple[str, bool, str]] = {
    "binance": ("Binance", False, "https://www.binance.com/en/my/settings/api-management"),
    "bybit": ("Bybit", False, "https://www.bybit.com/app/user/api-management"),
    "okx": ("OKX", True, "https://www.okx.com/account/my-api"),
    "kraken": ("Kraken", False, "https://pro.kraken.com/app/settings/api"),
    "kucoin": ("KuCoin", True, "https://www.kucoin.com/account/api"),
    "bitget": ("Bitget", True, "https://www.bitget.com/account/newapi"),
    "gate": ("Gate", False, "https://www.gate.io/myaccount/api_key_manage"),
    "mexc": ("MEXC", False, "https://www.mexc.com/user/openapi"),
    "htx": ("HTX", False, "https://www.htx.com/en-us/apikey/"),
}
# besides the default (spot / trading / unified) wallet, also read these
EXTRA_WALLETS = {"binance": ("funding",), "okx": ("funding",), "bybit": ("funding",), "kucoin": ("main",)}
STABLES = {"USDT", "USDC", "USD", "FDUSD", "DAI", "TUSD", "BUSD", "USDE", "PYUSD", "USDD", "USD1"}
DUST_USD = 1.0


class AccountError(RuntimeError):
    """A message that can be shown to the user as is."""


@dataclass(frozen=True, slots=True)
class Permissions:
    trade: bool | None  # None = the exchange doesn't tell
    withdraw: bool | None

    @property
    def label(self) -> str:
        if self.trade is None and self.withdraw is None:
            return "unknown"
        if self.withdraw:
            return "withdraw"
        return "read + trade" if self.trade else "read-only"


@dataclass(frozen=True, slots=True)
class Holding:
    asset: str
    amount: float
    price: float | None  # USD
    change_pct: float | None  # 24 h

    @property
    def usd(self) -> float | None:
        return self.amount * self.price if self.price is not None else None


@dataclass
class AccountView:
    exchange: str
    holdings: list[Holding] = field(default_factory=list)  # biggest first, dust excluded
    dust: int = 0  # assets worth less than $1
    unpriced: list[str] = field(default_factory=list)  # no USD market found
    error: str | None = None

    @property
    def total(self) -> float:
        return sum(h.usd or 0 for h in self.holdings)

    @property
    def change_usd(self) -> float:
        """Money made or lost in the last 24 h at today's amounts."""
        return sum(h.usd * h.change_pct / (100 + h.change_pct) for h in self.holdings
                   if h.usd is not None and h.change_pct is not None and h.change_pct > -100)


@dataclass
class Portfolio:
    accounts: list[AccountView]

    @property
    def total(self) -> float:
        return sum(a.total for a in self.accounts)

    @property
    def change_usd(self) -> float:
        return sum(a.change_usd for a in self.accounts)

    @property
    def change_pct(self) -> float | None:
        before = self.total - self.change_usd
        return self.change_usd / before * 100 if before > 0 else None

    def by_asset(self) -> list[Holding]:
        """The same coin on several exchanges, added up."""
        merged: dict[str, Holding] = {}
        for a in self.accounts:
            for h in a.holdings:
                prev = merged.get(h.asset)
                merged[h.asset] = h if prev is None else Holding(h.asset, prev.amount + h.amount, h.price, h.change_pct)
        return sorted(merged.values(), key=lambda h: h.usd or 0, reverse=True)


def parse_credentials(text: str, needs_password: bool) -> dict[str, str]:
    """'KEY SECRET [PASSPHRASE]' (spaces or new lines) -> ccxt credentials."""
    parts = text.split()
    expected = 3 if needs_password else 2
    if len(parts) != expected or any(len(p) < 6 for p in parts[:2]):
        what = "API key, secret and passphrase" if needs_password else "API key and secret"
        raise AccountError(f"Send the {what} in one message, separated by a space or a new line.")
    creds = {"apiKey": parts[0], "secret": parts[1]}
    if needs_password:
        creds["password"] = parts[2]
    return creds


def _default_factory(exchange: str, credentials: dict[str, str]) -> Any:
    return getattr(ccxt, exchange)({"enableRateLimit": True, "timeout": REQUEST_TIMEOUT * 1000, **credentials})


def _friendly(exc: BaseException, name: str) -> str:
    if isinstance(exc, ccxt.AuthenticationError):
        return f"{name} rejected the key. Check the key and secret (and passphrase), and that it isn't IP-restricted."
    if isinstance(exc, ccxt.PermissionDenied):
        return f"{name} says this key has no read permission."
    if isinstance(exc, (ccxt.NetworkError, asyncio.TimeoutError)):
        return f"Couldn't reach {name} right now. Try again in a minute."
    if isinstance(exc, AccountError):
        return str(exc)
    return f"{name} returned an error: {type(exc).__name__}"


class AccountReader:
    def __init__(self, factory: Callable[[str, dict[str, str]], Any] = _default_factory):
        self._factory = factory

    async def permissions(self, client: Any, exchange: str) -> Permissions:
        """Ask the exchange what the key may do (Binance, OKX, Bybit report it; others: unknown)."""
        try:
            if exchange == "binance":
                r = await client.sapi_get_account_apirestrictions()
                trade = bool(r.get("enableSpotAndMarginTrading") or r.get("enableFutures") or r.get("enableMargin"))
                return Permissions(trade, bool(r.get("enableWithdrawals")))
            if exchange == "okx":
                perm = (await client.private_get_account_config())["data"][0].get("perm", "")
                return Permissions("trade" in perm, "withdraw" in perm)
            if exchange == "bybit":
                r = (await client.private_get_v5_user_query_api())["result"]
                wallet = r.get("permissions", {}).get("Wallet", [])
                return Permissions(str(r.get("readOnly")) == "0", "Withdraw" in wallet)
        except Exception as exc:  # the check is a safety extra; don't fail the connection over it
            log.info("permission check on %s failed: %s", exchange, type(exc).__name__)
        return Permissions(None, None)

    async def _balances(self, client: Any, exchange: str) -> dict[str, float]:
        totals: dict[str, float] = {}
        for i, wallet in enumerate((None, *EXTRA_WALLETS.get(exchange, ()))):
            try:
                bal = await asyncio.wait_for(client.fetch_balance({"type": wallet} if wallet else {}), REQUEST_TIMEOUT * 2)
            except Exception:
                if i == 0:
                    raise  # the main wallet must work; extra ones are a bonus
                continue
            for asset, amount in (bal.get("total") or {}).items():
                if amount and amount > 0:
                    totals[asset.upper()] = totals.get(asset.upper(), 0.0) + float(amount)
        return totals

    async def _prices(self, client: Any, assets: list[str]) -> dict[str, tuple[float, float | None]]:
        await asyncio.wait_for(client.load_markets(), REQUEST_TIMEOUT * 2)
        symbols = {a: s for a in assets if (s := pick_symbol(client.markets, a))}
        if not symbols:
            return {}
        try:
            tickers = await asyncio.wait_for(client.fetch_tickers(list(symbols.values())), REQUEST_TIMEOUT * 2)
        except Exception:  # some exchanges can't fetch a list of tickers: one by one
            got = await asyncio.gather(*(client.fetch_ticker(s) for s in symbols.values()), return_exceptions=True)
            tickers = {s: t for s, t in zip(symbols.values(), got, strict=True) if isinstance(t, dict)}
        out = {}
        for asset, symbol in symbols.items():
            t = tickers.get(symbol)
            if t and t.get("last"):
                out[asset] = (float(t["last"]), t.get("percentage"))
        return out

    async def _read(self, client: Any, exchange: str) -> AccountView:
        balances = await self._balances(client, exchange)
        lookup = {a: (a[2:] if exchange == "binance" and a.startswith("LD") and len(a) > 3 else a) for a in balances}
        prices = await self._prices(client, sorted({c for c in lookup.values() if c not in STABLES}))
        merged: dict[str, float] = {}
        for asset, amount in balances.items():  # Binance Simple Earn "LDBTC" counts as BTC
            merged[lookup[asset]] = merged.get(lookup[asset], 0.0) + amount
        view = AccountView(exchange)
        for asset, amount in merged.items():
            price, change = (1.0, 0.0) if asset in STABLES else prices.get(asset, (None, None))
            if price is None:
                view.unpriced.append(asset)
                continue
            holding = Holding(asset, amount, price, change)
            if (holding.usd or 0) < DUST_USD:
                view.dust += 1
            else:
                view.holdings.append(holding)
        view.holdings.sort(key=lambda h: h.usd or 0, reverse=True)
        view.unpriced.sort()
        return view

    async def connect(self, exchange: str, credentials: dict[str, str]) -> tuple[AccountView, Permissions]:
        """Check a new key: it must read balances and must not be able to withdraw."""
        name = SUPPORTED[exchange][0]
        client = self._factory(exchange, credentials)
        try:
            perms = await self.permissions(client, exchange)
            if perms.withdraw:
                raise AccountError(
                    f"This {name} key can <b>withdraw funds</b>, so I won't store it. "
                    "Delete it on the exchange and create a new one with only “Read” enabled."
                )
            return await self._read(client, exchange), perms
        except Exception as exc:
            raise AccountError(_friendly(exc, name)) from exc
        finally:
            await client.close()

    async def read(self, exchange: str, credentials: dict[str, str]) -> AccountView:
        """Never raises: an account that can't be read comes back with `error` set."""
        client = self._factory(exchange, credentials)
        try:
            return await self._read(client, exchange)
        except Exception as exc:
            return AccountView(exchange, error=_friendly(exc, SUPPORTED.get(exchange, (exchange,))[0]))
        finally:
            await client.close()
