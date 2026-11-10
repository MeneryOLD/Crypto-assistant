"""Crypto Fear & Greed Index (alternative.me, free, updated daily)."""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass

import httpx

log = logging.getLogger(__name__)

URL = "https://api.alternative.me/fng/?limit=8"


@dataclass(frozen=True, slots=True)
class FearGreed:
    value: int  # 0 = extreme fear, 100 = extreme greed
    label: str
    yesterday: int | None
    week_ago: int | None

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
    )


class FearGreedClient:
    def __init__(self) -> None:
        self._cached: tuple[float, FearGreed | None] = (0.0, None)

    async def get(self) -> FearGreed | None:
        """Cached for 10 minutes; returns None if unavailable."""
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
