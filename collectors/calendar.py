"""Economic calendar (Forex Factory weekly feed, free): CPI, jobs, FOMC, GDP, central-bank speeches…

High-impact US releases are among the biggest scheduled crypto movers, so the bot shows them in the
digest, reminds users before they happen, and records them as events in its price memory.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

import httpx

log = logging.getLogger(__name__)

FEEDS = {
    "this": "https://nfs.faireconomy.media/ff_calendar_thisweek.json",
    "next": "https://nfs.faireconomy.media/ff_calendar_nextweek.json",
}
FLAGS = {
    "USD": "🇺🇸", "EUR": "🇪🇺", "GBP": "🇬🇧", "JPY": "🇯🇵", "CNY": "🇨🇳", "CAD": "🇨🇦",
    "AUD": "🇦🇺", "CHF": "🇨🇭", "NZD": "🇳🇿", "ALL": "🌐",
}
IMPACT_RANK = {"High": 3, "Medium": 2, "Low": 1, "Holiday": 0, "Non-Economic": 0}
IMPACT_ICON = {3: "🔴", 2: "🟠", 1: "🟡", 0: "⚪"}


@dataclass(frozen=True, slots=True)
class CalendarEvent:
    title: str
    country: str
    starts_at: datetime  # naive UTC
    impact: int  # 0..3
    forecast: str
    previous: str

    @property
    def flag(self) -> str:
        return FLAGS.get(self.country, self.country)


def parse(rows: list[dict[str, Any]]) -> list[CalendarEvent]:
    events = []
    for r in rows:
        try:
            starts = datetime.fromisoformat(r["date"]).astimezone(UTC).replace(tzinfo=None)
        except (KeyError, ValueError):
            continue
        title = (r.get("title") or "").strip()
        if not title:
            continue
        events.append(
            CalendarEvent(
                title=title[:150],
                country=(r.get("country") or "").strip()[:5],
                starts_at=starts,
                impact=IMPACT_RANK.get(r.get("impact") or "", 0),
                forecast=(r.get("forecast") or "").strip()[:20],
                previous=(r.get("previous") or "").strip()[:20],
            )
        )
    return events


async def fetch(week: str = "this", client: httpx.AsyncClient | None = None) -> list[CalendarEvent]:
    """Events of this or next week. Next week's file appears only late in the week (404 before)."""
    own = client is None
    client = client or httpx.AsyncClient(timeout=20, headers={"user-agent": "Mozilla/5.0 (compatible; CryptoIntelBot/1.0)"})
    try:
        resp = await client.get(FEEDS[week])
        if resp.status_code == 404:
            return []
        resp.raise_for_status()
        return parse(resp.json())
    except (httpx.HTTPError, ValueError) as exc:
        log.warning("economic calendar (%s week) unavailable: %s", week, exc)
        return []
    finally:
        if own:
            await client.aclose()
