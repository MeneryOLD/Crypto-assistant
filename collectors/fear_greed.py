"""Crypto Fear & Greed Index (alternative.me, free, updated daily), with 90 days of history."""

from __future__ import annotations

import logging
import statistics
import time
from dataclasses import dataclass, field

import httpx

log = logging.getLogger(__name__)

URL = "https://api.alternative.me/fng/?limit=90"


@dataclass(frozen=True, slots=True)
class FearGreed:
    value: int  # 0 = extreme fear, 100 = extreme greed
    label: str
    yesterday: int | None
    week_ago: int | None
    history: tuple[int, ...] = field(default=())  # newest first, up to 90 days

    @property
    def month_ago(self) -> int | None:
        return self.history[30] if len(self.history) > 30 else None

    @property
    def avg_30d(self) -> float | None:
        return statistics.fmean(self.history[:30]) if len(self.history) >= 7 else None

    @property
    def emoji(self) -> str:
        if self.value < 25:
            return "😱"
        if self.value < 45:
            return "😨"
        if self.value < 56:
            return "😐"
        if self.value < 75:
            return "😏"
        return "🤑"


def parse(data: dict) -> FearGreed | None:
    rows = data.get("data") or []
    if not rows:
        return None
    values = [int(r["value"]) for r in rows]
    return FearGreed(
        value=values[0],
        label=rows[0].get("value_classification", ""),
        yesterday=values[1] if len(values) > 1 else None,
        week_ago=values[7] if len(values) > 7 else None,
        history=tuple(values),
    )


class FearGreedClient:
    def __init__(self) -> None:
        self._cached: tuple[float, FearGreed | None] = (0.0, None)

    async def get(self) -> FearGreed | None:
        """Cached for 10 minutes; returns the last known value if the API is down."""
        ts, value = self._cached
        if value is not None and time.monotonic() - ts < 600:
            return value
        try:
            async with httpx.AsyncClient(timeout=10) as client:
                resp = await client.get(URL)
                resp.raise_for_status()
                value = parse(resp.json())
        except (httpx.HTTPError, ValueError, KeyError) as exc:
            log.warning("Fear & Greed unavailable: %s", exc)
            return self._cached[1]
        self._cached = (time.monotonic(), value)
        return value
