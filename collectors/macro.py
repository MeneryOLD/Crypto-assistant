"""Hourly history for the forecast and the "market context" block, from free public APIs (no keys):

Binance spot      hourly candles with taker-buy volume (who is buying aggressively)
Binance futures   premium index = perpetual vs. spot price (the futures basis: leverage demand)
Coinbase          BTC/ETH/SOL in USD: the "Coinbase premium" over Binance shows US demand
Yahoo Finance     S&P 500 and Nasdaq futures, dollar index, VIX, gold, US 10-year yield
DefiLlama         total stablecoin supply (money waiting on the sidelines)

Series are cached in memory and extended incrementally, so the hourly refresh needs only a few requests.
Every point is (open time in ms, value); a point is usable once its bar has closed.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field

import httpx

log = logging.getLogger(__name__)

HOUR_MS = 3_600_000
DAY_MS = 24 * HOUR_MS
BINANCE = "https://api.binance.com/api/v3/klines"
BINANCE_PREMIUM = "https://fapi.binance.com/fapi/v1/premiumIndexKlines"
COINBASE = "https://api.exchange.coinbase.com/products/{}-USD/candles"
YAHOO = "https://query1.finance.yahoo.com/v8/finance/chart/{}"
STABLES = "https://stablecoins.llama.fi/stablecoincharts/all"

# name -> (Yahoo symbol, label)
MACRO: dict[str, tuple[str, str]] = {
    "spx": ("ES=F", "S&P 500 futures"),
    "ndx": ("NQ=F", "Nasdaq futures"),
    "dxy": ("DX-Y.NYB", "US dollar index"),
    "vix": ("^VIX", "VIX (stock fear)"),
    "gold": ("GC=F", "Gold"),
    "us10y": ("^TNX", "US 10-year yield"),
}

Series = list[tuple[int, float]]


@dataclass
class Klines:
    """Hourly spot candles with the share bought by market (taker) orders."""

    times: list[int] = field(default_factory=list)
    closes: list[float] = field(default_factory=list)
    volumes: list[float] = field(default_factory=list)  # base asset
    taker_buy: list[float] = field(default_factory=list)  # base asset bought by takers


class MacroClient:
    def __init__(self, client: httpx.AsyncClient | None = None):
        self._http = client or httpx.AsyncClient(timeout=20, headers={"User-Agent": "Mozilla/5.0 (VexoraBot)"})
        self._cache: dict[str, Series] = {}
        self._klines: dict[str, Klines] = {}
        self._locks: dict[str, asyncio.Lock] = {}
        self._fetched_at: dict[str, float] = {}

    async def close(self) -> None:
        await self._http.aclose()

    async def _get(self, url: str, params: dict | None = None) -> object:
        for attempt in range(3):
            r = await self._http.get(url, params=params)
            if r.status_code == 429 and attempt < 2:
                await asyncio.sleep(2 * (attempt + 1))
                continue
            r.raise_for_status()
            return r.json()
        raise RuntimeError("unreachable")

    async def _extend(self, key: str, since_ms: int, fetch: Callable[[int], Awaitable[Series]], min_age: float = 300) -> Series:
        """Points since since_ms. The cache only grows: it is topped up from its last point, or refetched
        from since_ms when an older start is asked for."""
        lock = self._locks.setdefault(key, asyncio.Lock())
        async with lock:
            rows = self._cache.get(key, [])
            covers = bool(rows) and rows[0][0] <= since_ms + 2 * DAY_MS
            if not covers or time.monotonic() - self._fetched_at.get(key, 0) >= min_age:
                # re-fetch the last bars: the newest one may still have been open
                start = rows[-2][0] if covers and len(rows) > 2 else since_ms
                new = await fetch(start)
                merged = dict(rows)
                merged.update(new)
                self._cache[key] = rows = sorted(merged.items())
                self._fetched_at[key] = time.monotonic()
            return [r for r in rows if r[0] >= since_ms]

    # --- Binance -----------------------------------------------------------------------------

    async def klines(self, coin: str, since_ms: int) -> Klines:
        """Hourly BTCUSDT-style candles since since_ms, with taker-buy volume."""
        key = f"klines:{coin}"
        lock = self._locks.setdefault(key, asyncio.Lock())
        async with lock:
            k = self._klines.get(key) or Klines()
            covers = bool(k.times) and k.times[0] <= since_ms + 2 * DAY_MS
            if covers and time.monotonic() - self._fetched_at.get(key, 0) < 300:
                return _klines_since(k, since_ms)
            cursor = k.times[-2] if covers and len(k.times) > 2 else since_ms
            rows: dict[int, tuple[float, float, float]] = {
                t: (c, v, b) for t, c, v, b in zip(k.times, k.closes, k.volumes, k.taker_buy, strict=True)
            }
            now = int(time.time() * 1000)
            while cursor < now:
                batch = await self._get(BINANCE, {"symbol": f"{coin}USDT", "interval": "1h", "startTime": cursor, "limit": 1000})
                if not isinstance(batch, list) or not batch:
                    break
                for r in batch:
                    rows[int(r[0])] = (float(r[4]), float(r[5]), float(r[9]))
                cursor = int(batch[-1][0]) + HOUR_MS
                if len(batch) < 1000:
                    break
            out = Klines()
            for t in sorted(rows):
                if t + HOUR_MS <= now:  # closed candles only
                    c, v, b = rows[t]
                    out.times.append(t)
                    out.closes.append(c)
                    out.volumes.append(v)
                    out.taker_buy.append(b)
            self._klines[key] = out
            self._fetched_at[key] = time.monotonic()
            return _klines_since(out, since_ms)

    async def premium(self, coin: str, since_ms: int) -> Series:
        """Hourly perpetual premium over the spot index (fraction; positive = futures traders pay up)."""

        async def fetch(start: int) -> Series:
            out: Series = []
            cursor, now = start, int(time.time() * 1000)
            while cursor < now:
                batch = await self._get(BINANCE_PREMIUM, {"symbol": f"{coin}USDT", "interval": "1h", "startTime": cursor, "limit": 1500})
                if not isinstance(batch, list) or not batch:
                    break
                out += [(int(r[0]), float(r[4])) for r in batch]
                cursor = int(batch[-1][0]) + HOUR_MS
                if len(batch) < 1500:
                    break
            return out

        return await self._extend(f"premium:{coin}", since_ms, fetch)

    # --- Coinbase ----------------------------------------------------------------------------

    async def coinbase(self, coin: str, since_ms: int) -> Series:
        """Hourly USD closes on Coinbase (300 candles per request, fetched a few at a time)."""

        async def fetch(start: int) -> Series:
            now = int(time.time() * 1000)
            windows = list(range(start, now, 300 * HOUR_MS))
            sem = asyncio.Semaphore(4)

            async def one(a: int) -> Series:
                b = min(a + 300 * HOUR_MS, now)
                params = {"granularity": 3600, "start": _iso(a), "end": _iso(b)}
                async with sem:
                    try:
                        rows = await self._get(COINBASE.format(coin), params)
                    except Exception as exc:
                        log.debug("coinbase %s window failed: %s", coin, exc)
                        return []
                return [(int(r[0]) * 1000, float(r[4])) for r in rows] if isinstance(rows, list) else []

            parts = await asyncio.gather(*(one(a) for a in windows))
            return [p for part in parts for p in part]

        return await self._extend(f"coinbase:{coin}", since_ms, fetch)

    # --- Yahoo Finance -----------------------------------------------------------------------

    async def yahoo(self, name: str, since_ms: int) -> Series:
        """Hourly closes of a macro market (gaps when it's closed: nights, weekends)."""
        symbol = MACRO[name][0]

        async def fetch(start: int) -> Series:
            now = int(time.time())
            start_s = max(start // 1000, now - 729 * 86400)  # Yahoo keeps hourly bars for 730 days
            data = await self._get(YAHOO.format(symbol), {"interval": "1h", "period1": start_s, "period2": now})
            result = data["chart"]["result"][0]  # type: ignore[index]
            closes = result["indicators"]["quote"][0]["close"]
            return [(int(t) * 1000, float(c)) for t, c in zip(result.get("timestamp") or [], closes, strict=False) if c is not None]

        return await self._extend(f"yahoo:{name}", since_ms, fetch, min_age=600)

    async def macro_all(self, since_ms: int) -> dict[str, Series]:
        names = list(MACRO)
        got = await asyncio.gather(*(self.yahoo(n, since_ms) for n in names), return_exceptions=True)
        out = {}
        for n, g in zip(names, got, strict=True):
            if isinstance(g, BaseException):
                log.warning("macro %s unavailable: %s", n, g)
            else:
                out[n] = g
        return out

    # --- DefiLlama ---------------------------------------------------------------------------

    async def stablecoins(self, since_ms: int) -> Series:
        """Daily total stablecoin supply in USD."""

        async def fetch(start: int) -> Series:
            data = await self._get(STABLES)
            return [
                (int(d["date"]) * 1000, float(d["totalCirculatingUSD"]["peggedUSD"]))
                for d in data  # type: ignore[union-attr]
                if d.get("totalCirculatingUSD", {}).get("peggedUSD") and int(d["date"]) * 1000 >= start
            ]

        return await self._extend("stables", since_ms, fetch, min_age=3600)


def _klines_since(k: Klines, since_ms: int) -> Klines:
    from bisect import bisect_left

    a = bisect_left(k.times, since_ms)
    return Klines(k.times[a:], k.closes[a:], k.volumes[a:], k.taker_buy[a:])


def _iso(ms: int) -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(ms / 1000))


def value_at(series: Series, t: int, bar_ms: int = HOUR_MS, max_age_ms: int = 4 * DAY_MS) -> float | None:
    """Last value whose bar had closed by time t (no look-ahead), if not older than max_age_ms."""
    from bisect import bisect_right

    k = bisect_right(series, (t - bar_ms, float("inf"))) - 1
    if k < 0 or t - series[k][0] > max_age_ms:
        return None
    return series[k][1]


def change_at(series: Series, t: int, back_ms: int, bar_ms: int = HOUR_MS) -> float | None:
    """Fractional change between the values known at t - back_ms and at t."""
    now, before = value_at(series, t, bar_ms), value_at(series, t - back_ms, bar_ms)
    return now / before - 1 if now and before else None
