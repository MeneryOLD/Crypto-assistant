"""Background jobs that run while the bot is up."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable

from analysis.reactions import fill_price_marks
from bot.notifier import Notifier, score_items
from collectors.exchanges import ExchangeHub
from collectors.news import NewsCollector
from storage.db import Database

log = logging.getLogger(__name__)


async def run_every(name: str, seconds: float, job: Callable[[], Awaitable[None]]) -> None:
    """Run job forever, every `seconds`. One failure never stops the loop."""
    while True:
        try:
            await job()
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("job %s failed", name)
        await asyncio.sleep(seconds)


class NewsJob:
    """Checks the sources whose interval has passed, scores what's new and sends alerts."""

    def __init__(
        self, db: Database, collector: NewsCollector, kinds: set[str] | None = None, notifier: Notifier | None = None
    ):
        self.db = db
        self.collector = collector
        self.kinds = kinds
        self.notifier = notifier
        self.first_run = True

    async def __call__(self) -> None:
        articles = await self.collector.poll(only_due=not self.first_run, kinds=self.kinds)
        # Whatever is already in the feeds at startup is history, not breaking news.
        new = await self.db.add_news(articles, backfill=self.first_run)
        via = {a.url: a.via for a in articles}
        for item in new:
            if st := self.collector.status.get(via.get(item.url, "")):
                st.new_total += 1
        if new and not self.first_run:
            await score_items(self.db, new)  # the backfill is scored by ScoringJob instead
            for item in new:
                log.info("  NEW %s/10 [%s] %s: %s", item.importance, item.category, item.source, item.title[:110])
            if self.notifier:
                await self.notifier.notify(new)
        elif new:
            log.info("news: %d items loaded at startup", len(new))
        self.first_run = False


class ScoringJob:
    """Scores older items that have no importance yet (e.g. the startup backfill)."""

    def __init__(self, db: Database):
        self.db = db

    async def __call__(self) -> None:
        items = await self.db.unscored_news(limit=500)
        if items:
            await score_items(self.db, items)
            log.info("importance: scored %d older items", len(items))


class PriceMemoryJob:
    def __init__(self, db: Database, hub: ExchangeHub, exchange: str):
        self.db, self.hub, self.exchange = db, hub, exchange

    async def __call__(self) -> None:
        saved = await fill_price_marks(self.db, self.hub, self.exchange)
        if saved:
            log.info("price memory: recorded %d price points", saved)
