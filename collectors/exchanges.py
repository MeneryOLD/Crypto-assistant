"""Crypto exchange connections via ccxt (public market data only — no API keys, no trading)."""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

import ccxt.async_support as ccxt

log = logging.getLogger(__name__)

DEFAULT_EXCHANGES = ("binance", "bybit", "okx", "kraken")
QUOTES = ("USDT", "USDC", "USD")  # preferred quote currency, in order
REQUEST_TIMEOUT = 10  # seconds per exchange call


class ExchangeError(RuntimeError):
    """Unknown exchange/coin, or the exchange did not answer."""


@dataclass(frozen=True, slots=True)
class Ticker:
    exchange: str
    symbol: str
    last: float
    bid: float | None
    ask: float | None
    change_pct: float | None
    high: float | None
    low: float | None
    quote_volume: float | None


@dataclass(frozen=True, slots=True)
class OrderBook:
    exchange: str
    symbol: str
    bids: list[tuple[float, float]]  # (price, amount), best first
    asks: list[tuple[float, float]]


@dataclass(frozen=True, slots=True)
class ExchangeStatus:
    exchange: str
    ok: bool
    latency_ms: int | None
    markets: int
    error: str | None = None


@dataclass(frozen=True, slots=True)
class Spread:
    buy: Ticker  # lowest ask
    sell: Ticker  # highest bid
    pct: float  # gross difference, before fees and transfer costs


def pick_symbol(markets: dict[str, Any], coin: str) -> str | None:
    """Find the best spot pair for a coin, e.g. 'btc' -> 'BTC/USDT'."""
    coin = coin.upper()
    for quote in QUOTES:
        symbol = f"{coin}/{quote}"
        market = markets.get(symbol)
        if market and market.get("spot", True) and market.get("active") is not False:
            return symbol
    return None


def best_spread(tickers: list[Ticker]) -> Spread | None:
    """Where it's cheapest to buy and most expensive to sell right now."""
    asks = [t for t in tickers if t.ask]
    bids = [t for t in tickers if t.bid]
    if len(tickers) < 2 or not asks or not bids:
        return None
    buy = min(asks, key=lambda t: t.ask)  # type: ignore[arg-type, return-value]
    sell = max(bids, key=lambda t: t.bid)  # type: ignore[arg-type, return-value]
    if buy.exchange == sell.exchange:
        return Spread(buy, sell, 0.0)
    return Spread(buy, sell, (sell.bid - buy.ask) / buy.ask * 100)  # type: ignore[operator]


def _default_factory(name: str) -> Any:
    return getattr(ccxt, name)({"enableRateLimit": True, "timeout": REQUEST_TIMEOUT * 1000})


class ExchangeHub:
    """Keeps one connection per exchange and loads its market list once."""

    def __init__(self, names: list[str], factory: Callable[[str], Any] = _default_factory):
        unknown = [n for n in names if n not in ccxt.exchanges]
        if unknown:
            raise ValueError(f"Unknown exchange(s): {', '.join(unknown)}")
        self.names = list(names)
        self._factory = factory
        self._clients: dict[str, Any] = {}
        self._markets_ready: dict[str, asyncio.Lock] = {n: asyncio.Lock() for n in names}

    async def close(self) -> None:
        await asyncio.gather(*(c.close() for c in self._clients.values()), return_exceptions=True)

    async def is_listed(self, coin: str) -> bool:
        """Is this coin traded on the first connected exchange? (Used to tell coins from search words.)"""
        try:
            await self._symbol(self.names[0], coin)
        except ExchangeError:
            return False
        return True

    def find(self, name: str) -> str | None:
        name = name.lower().strip()
        return name if name in self.names else None

    async def _client(self, name: str) -> Any:
        if name not in self.names:
            raise ExchangeError(f"Exchange '{name}' is not connected")
        client = self._clients.get(name)
        if client is None:
            client = self._clients[name] = self._factory(name)
        async with self._markets_ready[name]:
            if not client.markets:
                try:
                    await asyncio.wait_for(client.load_markets(), REQUEST_TIMEOUT * 2)
                except Exception as exc:
                    raise ExchangeError(f"{name}: {_short(exc)}") from exc
        return client

    async def _symbol(self, name: str, coin: str) -> tuple[Any, str]:
        client = await self._client(name)
        symbol = pick_symbol(client.markets, coin)
        if symbol is None:
            raise ExchangeError(f"{coin.upper()} is not traded on {name}")
        return client, symbol

    async def ticker(self, name: str, coin: str) -> Ticker:
        client, symbol = await self._symbol(name, coin)
        try:
            t = await asyncio.wait_for(client.fetch_ticker(symbol), REQUEST_TIMEOUT)
        except Exception as exc:
            raise ExchangeError(f"{name}: {_short(exc)}") from exc
        if t.get("last") is None:
            raise ExchangeError(f"{name}: no recent trades for {symbol}")
        return Ticker(
            exchange=name,
            symbol=symbol,
            last=float(t["last"]),
            bid=t.get("bid"),
            ask=t.get("ask"),
            change_pct=t.get("percentage"),
            high=t.get("high"),
            low=t.get("low"),
            quote_volume=t.get("quoteVolume"),
        )

    async def tickers_everywhere(self, coin: str) -> tuple[list[Ticker], dict[str, str]]:
        """Ticker from every connected exchange. Returns (tickers, {exchange: error})."""
        results = await asyncio.gather(*(self.ticker(n, coin) for n in self.names), return_exceptions=True)
        tickers, errors = [], {}
        for name, r in zip(self.names, results, strict=False):
            if isinstance(r, Ticker):
                tickers.append(r)
            else:
                errors[name] = str(r) if isinstance(r, ExchangeError) else _short(r)
        return tickers, errors

    async def order_book(self, name: str, coin: str, depth: int = 5) -> OrderBook:
        client, symbol = await self._symbol(name, coin)
        # Some exchanges only accept specific depth values; ask for more and trim.
        try:
            ob = await asyncio.wait_for(client.fetch_order_book(symbol, limit=max(depth, 20)), REQUEST_TIMEOUT)
        except Exception as exc:
            raise ExchangeError(f"{name}: {_short(exc)}") from exc
        return OrderBook(
            exchange=name,
            symbol=symbol,
            bids=[(float(p), float(a)) for p, a, *_ in ob["bids"][:depth]],
            asks=[(float(p), float(a)) for p, a, *_ in ob["asks"][:depth]],
        )

    async def candles(
        self, name: str, coin: str, since_ms: int, limit: int = 1000, timeframe: str = "1m"
    ) -> list[list[float]]:
        """Candles [timestamp_ms, open, high, low, close, volume] starting at since_ms (1-minute by default)."""
        client, symbol = await self._symbol(name, coin)
        try:
            return await asyncio.wait_for(client.fetch_ohlcv(symbol, timeframe, since=since_ms, limit=limit), REQUEST_TIMEOUT)
        except Exception as exc:
            raise ExchangeError(f"{name}: {_short(exc)}") from exc

    async def funding_history(self, name: str, coin: str, since_ms: int) -> list[tuple[int, float]]:
        """[(timestamp_ms, rate)] of the perpetual's funding rate since since_ms (every 8 h on most exchanges)."""
        client = await self._client(name)
        symbol = f"{coin.upper()}/USDT:USDT"
        if symbol not in client.markets:
            return []
        out: list[tuple[int, float]] = []
        cursor = since_ms
        try:
            for _ in range(5):  # 1000 records per call ≈ 333 days of 8-hour fundings
                rows = await asyncio.wait_for(client.fetch_funding_rate_history(symbol, since=cursor, limit=1000), REQUEST_TIMEOUT)
                rows = [(int(r["timestamp"]), float(r["fundingRate"])) for r in rows if r.get("fundingRate") is not None]
                if not rows:
                    break
                out.extend(rows)
                if len(rows) < 1000:
                    break
                cursor = rows[-1][0] + 1
        except Exception as exc:
            raise ExchangeError(f"{name}: {_short(exc)}") from exc
        return out

    async def change_7d(self, name: str, coin: str) -> float | None:
        """Percent change over the last 7 days (daily candles)."""
        client, symbol = await self._symbol(name, coin)
        try:
            candles = await asyncio.wait_for(client.fetch_ohlcv(symbol, "1d", limit=8), REQUEST_TIMEOUT)
        except Exception:
            return None
        if len(candles) < 8 or not candles[0][4]:
            return None
        return (float(candles[-1][4]) - float(candles[0][4])) / float(candles[0][4]) * 100

    async def funding_rates(self, coin: str) -> dict[str, float]:
        """Perpetual-futures funding rate per exchange (positive = longs pay shorts = crowded longs)."""
        symbol = f"{coin.upper()}/USDT:USDT"

        async def one(name: str) -> tuple[str, float | None]:
            try:
                client = await self._client(name)
                if symbol not in client.markets or not client.has.get("fetchFundingRate"):
                    return name, None
                fr = await asyncio.wait_for(client.fetch_funding_rate(symbol), REQUEST_TIMEOUT)
                rate = fr.get("fundingRate")
                return name, float(rate) if rate is not None else None
            except Exception:
                return name, None

        results = await asyncio.gather(*(one(n) for n in self.names))
        return {name: rate for name, rate in results if rate is not None}

    async def status(self) -> list[ExchangeStatus]:
        async def one(name: str) -> ExchangeStatus:
            start = time.monotonic()
            try:
                client = await self._client(name)
                await asyncio.wait_for(client.fetch_ticker(pick_symbol(client.markets, "BTC") or "BTC/USDT"), REQUEST_TIMEOUT)
            except Exception as exc:
                return ExchangeStatus(name, False, None, 0, str(exc) if isinstance(exc, ExchangeError) else _short(exc))
            return ExchangeStatus(name, True, int((time.monotonic() - start) * 1000), len(client.markets))

        return list(await asyncio.gather(*(one(n) for n in self.names)))


def _short(exc: BaseException) -> str:
    if isinstance(exc, asyncio.TimeoutError):
        return "timed out"
    text = str(exc).strip() or type(exc).__name__
    if isinstance(exc, ccxt.NetworkError) and len(text) > 120:
        return "network error / not reachable from this location"
    return text[:120]
