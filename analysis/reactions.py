"""Records how BTC/ETH moved after every news item, using 1-minute exchange candles.

Because prices come from historical candles, nothing is lost if the bot was offline:
missing points are filled in when it starts again.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime

from collectors.exchanges import ExchangeError, ExchangeHub
from storage.db import Database

log = logging.getLogger(__name__)

MINUTE_MS = 60_000
CANDLES_PER_CALL = 1000


def minute_ms(ts: datetime) -> int:
    """Start of the minute containing ts (naive UTC) in epoch milliseconds."""
    ms = int(ts.replace(tzinfo=UTC).timestamp() * 1000)
    return ms - ms % MINUTE_MS


async def prices_at(hub: ExchangeHub, exchange: str, coin: str, timestamps: list[datetime]) -> dict[int, float]:
    """Price (open of that minute's candle) for each timestamp, keyed by minute_ms."""
    wanted = sorted({minute_ms(t) for t in timestamps})
    found: dict[int, float] = {}
    i = 0
    while i < len(wanted):
        start = wanted[i]
        candles = await hub.candles(exchange, coin, start, CANDLES_PER_CALL)
        by_minute = {int(c[0]): float(c[1]) for c in candles}
        end = start + CANDLES_PER_CALL * MINUTE_MS
        while i < len(wanted) and wanted[i] < end:
            if wanted[i] in by_minute:
                found[wanted[i]] = by_minute[wanted[i]]
            i += 1
        if not candles:
            break
    return found


async def fill_price_marks(db: Database, hub: ExchangeHub, exchange: str = "binance") -> int:
    """Record every due price point. Returns how many were saved."""
    due = await db.missing_marks()
    if not due:
        return 0
    by_asset: dict[str, list[tuple[int, int, datetime]]] = {}
    for news_id, asset, minutes, ts in due:
        by_asset.setdefault(asset, []).append((news_id, minutes, ts))

    marks = []
    for asset, rows in by_asset.items():
        try:
            prices = await prices_at(hub, exchange, asset, [ts for _, _, ts in rows])
        except ExchangeError as exc:
            log.warning("price history for %s unavailable: %s", asset, exc)
            continue
        for news_id, minutes, ts in rows:
            if (price := prices.get(minute_ms(ts))) is not None:
                marks.append((news_id, asset, minutes, price))
    await db.add_marks(marks)
    return len(marks)
