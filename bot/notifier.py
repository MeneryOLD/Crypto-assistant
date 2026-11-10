"""Scores new news and pushes the important ones to subscribers within seconds."""

from __future__ import annotations

import asyncio
import logging
from datetime import timedelta

from aiogram import Bot
from aiogram.exceptions import TelegramAPIError, TelegramForbiddenError

from analysis.importance import score
from analysis.patterns import PatternCache, same_story
from bot import screens
from collectors.exchanges import ExchangeHub, Ticker
from storage.db import Database, NewsItem, User, utcnow

log = logging.getLogger(__name__)

MAX_PER_HOUR = 12  # per user, so a hectic news day never floods the chat
FRESH_MINUTES = 90  # only alert about news published in the last 90 minutes
CONFIRM_WINDOW = timedelta(hours=6)


async def score_items(db: Database, items: list[NewsItem]) -> None:
    """Score items (in place) using other news from the surrounding hours as confirmation."""
    if not items:
        return
    start = min(i.event_at for i in items) - CONFIRM_WINDOW
    end = max(i.event_at for i in items) + timedelta(minutes=10)
    nearby = await db.news_between(start, end)
    results = {}
    for item in items:
        window = [n for n in nearby if abs((n.event_at - item.event_at).total_seconds()) <= CONFIRM_WINDOW.total_seconds()]
        s = score(item, window)
        item.importance, item.importance_reasons = s.value, " · ".join(s.reasons)
        results[item.id] = (s.value, item.importance_reasons)
    await db.set_importance(results)


def wants(user: User, item: NewsItem) -> bool:
    if (item.importance or 0) < user.alert_level:
        return False
    return not user.categories or item.category in user.categories


class Notifier:
    def __init__(self, bot: Bot, db: Database, hub: ExchangeHub, patterns: PatternCache):
        self.bot, self.db, self.hub, self.patterns = bot, db, hub, patterns

    async def alert_text(self, item: NewsItem) -> str:
        btc, pattern = await asyncio.gather(
            self.hub.ticker(self.hub.names[0], "BTC"), self.patterns.for_item(item), return_exceptions=True
        )
        return screens.alert_message(
            item,
            btc if isinstance(btc, Ticker) else None,
            pattern if not isinstance(pattern, BaseException) else None,
        )

    async def send(self, user: User, item: NewsItem, *, test: bool = False) -> bool:
        text = await self.alert_text(item)
        if test:
            text = "🧪 <i>Test alert — this is how notifications look:</i>\n\n" + text
        try:
            await self.bot.send_message(user.telegram_id, text, reply_markup=screens.alert_buttons(item))
        except TelegramForbiddenError:
            log.info("user %s blocked the bot; turning alerts off", user.telegram_id)
            await self.db.update_user(user.telegram_id, alerts_on=False)
            return False
        except TelegramAPIError as exc:
            log.warning("alert to %s failed: %s", user.telegram_id, exc)
            return False
        return True

    async def notify(self, items: list[NewsItem]) -> int:
        """Send fresh, important items to every subscriber whose settings match. Returns alerts sent."""
        fresh_after = utcnow() - timedelta(minutes=FRESH_MINUTES)
        candidates = sorted(
            (i for i in items if not i.backfill and i.importance and i.event_at >= fresh_after),
            key=lambda i: -(i.importance or 0),
        )
        if not candidates:
            return 0
        sent = 0
        for user in await self.db.alert_subscribers():
            matching = [i for i in candidates if wants(user, i)]
            if not matching:
                continue
            already = await self.db.recent_alerts(user.id)
            budget = MAX_PER_HOUR - await self.db.count_alerts_since(user.id, utcnow() - timedelta(hours=1))
            for item in matching:
                if budget <= 0:
                    break
                if any(same_story(item, prev) for prev in already):
                    continue  # this story was already sent (from another outlet)
                if await self.send(user, item):
                    await self.db.record_alert(user.id, item.id)
                    already.append(item)
                    budget -= 1
                    sent += 1
                await asyncio.sleep(0.05)
        if sent:
            log.info("alerts: sent %d", sent)
        return sent
