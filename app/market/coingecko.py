"""CoinGecko market data provider."""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass

import httpx

log = logging.getLogger(__name__)

BASE_URL = "https://api.coingecko.com/api/v3"


class MarketDataError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class CoinMarket:
    coin_id: str
    symbol: str
    name: str
    price_usd: float
    rank: int | None = None
    market_cap: float | None = None
    volume_24h: float | None = None
    change_1h: float | None = None
    change_24h: float | None = None
    change_7d: float | None = None


@dataclass(frozen=True, slots=True)
class GlobalMarket:
    total_market_cap_usd: float
    total_volume_usd: float
    market_cap_change_24h: float
    btc_dominance: float
    eth_dominance: float


@dataclass(frozen=True, slots=True)
class CoinRef:
    coin_id: str
    symbol: str
    name: str


def _parse_market(row: dict) -> CoinMarket:
    return CoinMarket(
        coin_id=row["id"],
        symbol=(row.get("symbol") or "").upper(),
        name=row.get("name") or row["id"],
        price_usd=float(row.get("current_price") or 0.0),
        rank=row.get("market_cap_rank"),
        market_cap=row.get("market_cap"),
        volume_24h=row.get("total_volume"),
        change_1h=row.get("price_change_percentage_1h_in_currency"),
        change_24h=row.get("price_change_percentage_24h_in_currency", row.get("price_change_percentage_24h")),
        change_7d=row.get("price_change_percentage_7d_in_currency"),
    )


class CoinGeckoClient:
    """Thin async wrapper with retries on rate limiting and a short response cache."""

    def __init__(self, api_key: str | None = None, *, cache_ttl: float = 60.0, client: httpx.AsyncClient | None = None):
        headers = {"accept": "application/json", "user-agent": "crypto-intelligence-bot/1.0"}
        if api_key:
            headers["x-cg-demo-api-key"] = api_key
        self._client = client or httpx.AsyncClient(base_url=BASE_URL, headers=headers, timeout=20.0)
        self._cache: dict[str, tuple[float, object]] = {}
        self._cache_ttl = cache_ttl

    async def close(self) -> None:
        await self._client.aclose()

    async def _get(self, path: str, params: dict | None = None) -> object:
        key = path + "?" + "&".join(f"{k}={v}" for k, v in sorted((params or {}).items()))
        cached = self._cache.get(key)
        if cached and time.monotonic() - cached[0] < self._cache_ttl:
            return cached[1]

        delay = 2.0
        for attempt in range(4):
            try:
                resp = await self._client.get(path, params=params)
            except httpx.HTTPError as exc:
                if attempt == 3:
                    raise MarketDataError(f"CoinGecko request failed: {exc}") from exc
            else:
                if resp.status_code == 429 or resp.status_code >= 500:
                    retry_after = resp.headers.get("retry-after")
                    wait = float(retry_after) if retry_after and retry_after.isdigit() else delay
                    log.warning("CoinGecko %s on %s; retrying in %.0fs", resp.status_code, path, wait)
                    if attempt == 3:
                        raise MarketDataError(f"CoinGecko unavailable (HTTP {resp.status_code})")
                    await asyncio.sleep(min(wait, 30))
                    delay *= 2
                    continue
                if resp.is_error:
                    raise MarketDataError(f"CoinGecko HTTP {resp.status_code}: {resp.text[:200]}")
                data = resp.json()
                self._cache[key] = (time.monotonic(), data)
                return data
            await asyncio.sleep(delay)
            delay *= 2
        raise MarketDataError("CoinGecko request failed")

    async def top_markets(self, count: int = 20) -> list[CoinMarket]:
        data = await self._get(
            "/coins/markets",
            {
                "vs_currency": "usd",
                "order": "market_cap_desc",
                "per_page": count,
                "page": 1,
                "price_change_percentage": "1h,24h,7d",
            },
        )
        return [_parse_market(row) for row in data]  # type: ignore[union-attr]

    async def markets_for(self, coin_ids: list[str]) -> list[CoinMarket]:
        if not coin_ids:
            return []
        data = await self._get(
            "/coins/markets",
            {
                "vs_currency": "usd",
                "ids": ",".join(sorted(set(coin_ids))),
                "price_change_percentage": "1h,24h,7d",
            },
        )
        return [_parse_market(row) for row in data]  # type: ignore[union-attr]

    async def global_market(self) -> GlobalMarket:
        data = (await self._get("/global"))["data"]  # type: ignore[index]
        pct = data.get("market_cap_percentage", {})
        return GlobalMarket(
            total_market_cap_usd=float(data["total_market_cap"]["usd"]),
            total_volume_usd=float(data["total_volume"]["usd"]),
            market_cap_change_24h=float(data.get("market_cap_change_percentage_24h_usd") or 0.0),
            btc_dominance=float(pct.get("btc", 0.0)),
            eth_dominance=float(pct.get("eth", 0.0)),
        )

    async def search(self, query: str) -> list[CoinRef]:
        data = await self._get("/search", {"query": query})
        coins = data.get("coins", [])  # type: ignore[union-attr]
        return [CoinRef(c["id"], (c.get("symbol") or "").upper(), c.get("name") or c["id"]) for c in coins]


class CoinResolver:
    """Maps user input like 'btc', 'Bitcoin' or 'bitcoin' to a CoinGecko id."""

    def __init__(self, client: CoinGeckoClient, tracked_count: int = 20):
        self._client = client
        self._tracked_count = tracked_count

    async def resolve(self, query: str) -> CoinRef | None:
        q = query.strip().lower()
        if not q:
            return None
        try:
            top = await self._client.top_markets(self._tracked_count)
        except MarketDataError:
            top = []
        match = match_coin(q, [CoinRef(c.coin_id, c.symbol, c.name) for c in top])
        if match:
            return match
        results = await self._client.search(q)
        return match_coin(q, results) or (results[0] if results else None)


def match_coin(query: str, coins: list[CoinRef]) -> CoinRef | None:
    q = query.strip().lower()
    for attr in ("coin_id", "symbol", "name"):
        for coin in coins:
            if getattr(coin, attr).lower() == q:
                return coin
    return None
