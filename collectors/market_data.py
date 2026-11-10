"""Market-wide data: global crypto market, futures positioning, stablecoin liquidity, Bitcoin network.

All sources are free and need no keys. Every method returns None when a source is down, so one
failing API never breaks a screen. Results are cached (TTL per data type) to respect rate limits.
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass
from typing import Any

import httpx

log = logging.getLogger(__name__)

COINGECKO_GLOBAL = "https://api.coingecko.com/api/v3/global"
BINANCE_FAPI = "https://fapi.binance.com"
BYBIT_RATIO = "https://api.bybit.com/v5/market/account-ratio"
STABLECOINS = "https://stablecoins.llama.fi/stablecoins?includePrices=false"
MEMPOOL_FEES = "https://mempool.space/api/v1/fees/recommended"
BLOCKCHAIN_STATS = "https://api.blockchain.info/stats"


@dataclass(frozen=True, slots=True)
class GlobalMarket:
    total_mcap: float
    total_volume: float
    mcap_change_24h: float
    btc_dominance: float
    eth_dominance: float


@dataclass(frozen=True, slots=True)
class Positioning:
    """Who is long and who is short on BTC perpetual futures."""

    long_short_ratio: float | None  # Binance, all accounts
    long_pct: float | None  # share of Binance accounts that are long (0..1)
    top_trader_ratio: float | None  # Binance top traders (by position size)
    bybit_buy_ratio: float | None  # share of Bybit accounts that are long (0..1)
    open_interest_btc: float | None  # Binance BTCUSDT open interest, in BTC
    oi_change_24h: float | None  # % change of open interest over 24 h


@dataclass(frozen=True, slots=True)
class Liquidity:
    """Total USD stablecoin supply: new money ready to buy crypto."""

    supply: float
    change_1d: float | None
    change_7d: float | None
    change_30d: float | None


@dataclass(frozen=True, slots=True)
class Network:
    fastest_fee: int | None  # sat/vB
    hour_fee: int | None
    hashrate_ehs: float | None  # exahashes per second
    tx_24h: int | None


def _pct(now: float, before: float | None) -> float | None:
    return (now - before) / before * 100 if before else None


def parse_global(data: dict[str, Any]) -> GlobalMarket:
    d = data["data"]
    pct = d.get("market_cap_percentage", {})
    return GlobalMarket(
        total_mcap=float(d["total_market_cap"]["usd"]),
        total_volume=float(d["total_volume"]["usd"]),
        mcap_change_24h=float(d.get("market_cap_change_percentage_24h_usd") or 0.0),
        btc_dominance=float(pct.get("btc", 0.0)),
        eth_dominance=float(pct.get("eth", 0.0)),
    )


def parse_stablecoins(data: dict[str, Any]) -> Liquidity:
    now = day = week = month = 0.0
    for asset in data.get("peggedAssets", []):
        if asset.get("pegType") != "peggedUSD":
            continue
        now += float((asset.get("circulating") or {}).get("peggedUSD") or 0)
        day += float((asset.get("circulatingPrevDay") or {}).get("peggedUSD") or 0)
        week += float((asset.get("circulatingPrevWeek") or {}).get("peggedUSD") or 0)
        month += float((asset.get("circulatingPrevMonth") or {}).get("peggedUSD") or 0)
    return Liquidity(now, _pct(now, day), _pct(now, week), _pct(now, month))


def parse_positioning(
    global_ratio: list[dict[str, Any]] | None,
    top_ratio: list[dict[str, Any]] | None,
    bybit: dict[str, Any] | None,
    oi_hist: list[dict[str, Any]] | None,
) -> Positioning:
    ls = lp = top = by = oi = oi_chg = None
    if global_ratio:
        ls, lp = float(global_ratio[-1]["longShortRatio"]), float(global_ratio[-1]["longAccount"])
    if top_ratio:
        top = float(top_ratio[-1]["longShortRatio"])
    if bybit and (rows := (bybit.get("result") or {}).get("list")):
        by = float(rows[0]["buyRatio"])
    if oi_hist:
        oi = float(oi_hist[-1]["sumOpenInterest"])
        oi_chg = _pct(oi, float(oi_hist[0]["sumOpenInterest"]))
    return Positioning(ls, lp, top, by, oi, oi_chg)


class MarketDataClient:
    def __init__(self, client: httpx.AsyncClient | None = None):
        self._http = client or httpx.AsyncClient(timeout=15, headers={"user-agent": "Mozilla/5.0 (compatible; CryptoIntelBot/1.0)"})
        self._cache: dict[str, tuple[float, Any]] = {}

    async def close(self) -> None:
        await self._http.aclose()

    async def _json(self, url: str, ttl: float, params: dict[str, Any] | None = None) -> Any:
        key = url + str(sorted((params or {}).items()))
        hit = self._cache.get(key)
        if hit and time.monotonic() - hit[0] < ttl:
            return hit[1]
        try:
            resp = await self._http.get(url, params=params)
            resp.raise_for_status()
            data = resp.json()
        except (httpx.HTTPError, ValueError) as exc:
            log.warning("market data %s unavailable: %s", url.split("/")[2], exc)
            return hit[1] if hit else None  # serve stale data rather than nothing
        self._cache[key] = (time.monotonic(), data)
        return data

    async def global_market(self) -> GlobalMarket | None:
        data = await self._json(COINGECKO_GLOBAL, ttl=300)
        try:
            return parse_global(data) if data else None
        except (KeyError, TypeError, ValueError):
            return None

    async def positioning(self, symbol: str = "BTCUSDT") -> Positioning | None:
        params = {"symbol": symbol, "period": "1h", "limit": 2}
        results = await asyncio.gather(
            self._json(f"{BINANCE_FAPI}/futures/data/globalLongShortAccountRatio", 300, params),
            self._json(f"{BINANCE_FAPI}/futures/data/topLongShortPositionRatio", 300, params),
            self._json(BYBIT_RATIO, 300, {"category": "linear", "symbol": symbol, "period": "1h", "limit": 1}),
            self._json(f"{BINANCE_FAPI}/futures/data/openInterestHist", 300, {"symbol": symbol, "period": "1h", "limit": 25}),
        )
        try:
            p = parse_positioning(*results)
        except (KeyError, TypeError, ValueError, IndexError):
            return None
        return None if all(v is None for v in (p.long_short_ratio, p.bybit_buy_ratio, p.open_interest_btc)) else p

    async def liquidity(self) -> Liquidity | None:
        data = await self._json(STABLECOINS, ttl=1800)
        try:
            return parse_stablecoins(data) if data else None
        except (KeyError, TypeError, ValueError):
            return None

    async def network(self) -> Network | None:
        fees, stats = await asyncio.gather(self._json(MEMPOOL_FEES, 300), self._json(BLOCKCHAIN_STATS, 900))
        if not fees and not stats:
            return None
        hashrate = (stats or {}).get("hash_rate")  # GH/s
        return Network(
            fastest_fee=(fees or {}).get("fastestFee"),
            hour_fee=(fees or {}).get("hourFee"),
            hashrate_ehs=float(hashrate) / 1e9 if hashrate else None,
            tx_24h=(stats or {}).get("n_tx"),
        )
