"""Starts the bot and its background jobs.  Run with:  python -m bot.main"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from logging.handlers import RotatingFileHandler

from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.types import BotCommand

from analysis.patterns import PatternCache
from analysis.similar import SimilarityEngine
from bot.context import App
from bot.handlers import fallback_router
from bot.handlers import router as main_router
from bot.handlers_insights import router as insights_router
from bot.handlers_settings import router as settings_router
from bot.jobs import (
    CalendarJob,
    DigestJob,
    HistoryImportJob,
    NewsJob,
    PriceMemoryJob,
    ReminderJob,
    RetentionJob,
    ScoringJob,
    SnapshotJob,
)
from bot.notifier import Notifier
from bot.scheduler import Scheduler
from collectors.exchanges import ExchangeHub
from collectors.fear_greed import FearGreedClient
from collectors.market_data import MarketDataClient
from collectors.news import SOURCES, NewsCollector, premium_sources
from config import Settings, get_settings
from storage.db import Database

log = logging.getLogger("bot")

COMMANDS = [
    BotCommand(command="home", description="🏠 Home dashboard"),
    BotCommand(command="top", description="🔥 Most important news"),
    BotCommand(command="news", description="📰 World news"),
    BotCommand(command="sentiment", description="📊 Market mood & positioning"),
    BotCommand(command="calendar", description="📅 Economic calendar"),
    BotCommand(command="markets", description="💹 Coin prices on exchanges"),
    BotCommand(command="history", description="🧠 How BTC moved after news"),
    BotCommand(command="similar", description="🧠 What happened after similar news"),
    BotCommand(command="settings", description="⚙️ Alerts, quiet hours, brief, time zone"),
    BotCommand(command="status", description="📡 Sources, exchanges, jobs"),
    BotCommand(command="help", description="❓ How to use"),
]


def setup_logging(settings: Settings) -> None:
    handlers: list[logging.Handler] = [logging.StreamHandler()]
    if settings.log_file:
        handlers.append(RotatingFileHandler(settings.log_file, maxBytes=5_000_000, backupCount=5, encoding="utf-8"))
    logging.basicConfig(
        level=settings.log_level.upper(),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        handlers=handlers,
    )
    logging.getLogger("httpx").setLevel(logging.WARNING)  # don't log every feed request


async def main() -> None:
    settings = get_settings()
    setup_logging(settings)

    db = Database(settings.db_path)
    await db.init()
    exchanges = settings.exchange_list
    if settings.price_history_exchange not in exchanges:
        exchanges.append(settings.price_history_exchange)
    hub = ExchangeHub(exchanges)
    sources = SOURCES + premium_sources(settings.cryptopanic_token, settings.x_bearer_token, settings.x_account_list)
    collector = NewsCollector(sources=sources, x_bearer_token=settings.x_bearer_token)

    bot = Bot(
        settings.bot_token,
        default=DefaultBotProperties(parse_mode=ParseMode.HTML, link_preview_is_disabled=True),
    )
    app = App(
        settings=settings,
        db=db,
        hub=hub,
        collector=collector,
        fg=FearGreedClient(),
        market=MarketDataClient(),
        similar=SimilarityEngine(db),
        patterns=PatternCache(db),
    )
    app.notifier = Notifier(bot, db, hub, app.patterns, app.similar)

    # Objects passed here are available to every handler by parameter name.
    dp = Dispatcher(db=db, hub=hub, collector=collector, fg=app.fg, notifier=app.notifier, app=app)
    dp.include_router(settings_router)  # /start, home, settings
    dp.include_router(main_router)
    dp.include_router(insights_router)
    dp.include_router(fallback_router)  # last: free text = coin lookup or news search

    history_job = HistoryImportJob(db, enabled=settings.history_import)
    live_kinds = {"rss", "google", "reddit", "cryptopanic", "x"}
    s = app.scheduler = Scheduler()
    s.add("news-fast", 3, NewsJob(db, collector, {"telegram"}, app.notifier), description="Telegram channels")
    s.add("news", 5, NewsJob(db, collector, live_kinds, app.notifier), description="RSS, Google News, Reddit, APIs")
    s.add("scoring", 20, ScoringJob(db), first_delay=10, description="importance + tone")
    s.add("price-memory", 60, PriceMemoryJob(db, app, settings.price_history_exchange), first_delay=15)
    s.add("history-import", 8, history_job, first_delay=20, description="90-day news archive (GDELT)")
    s.add("calendar", 1800, CalendarJob(db), first_delay=5, description="economic calendar")
    s.add("reminders", 60, ReminderJob(db, bot), first_delay=30, description="30 min before big releases")
    s.add("mood-snapshot", 3600, SnapshotJob(app), first_delay=60, description="hourly market mood")
    s.add("digest", 60, DigestJob(app, bot), first_delay=45, description="morning briefs")
    s.add("retention", 86400, RetentionJob(db, settings.retention_days), first_delay=600, description="delete old data")

    s.start()
    try:
        await bot.set_my_commands(COMMANDS)  # the "Menu" button in Telegram
        me = await bot.get_me()
        log.info(
            "Bot @%s started: %d news sources, %d exchanges, %d background jobs. Press Ctrl+C to stop.",
            me.username, len(collector.sources), len(hub.names), len(s.jobs),
        )
        await dp.start_polling(bot)
    finally:
        await s.stop()
        await history_job.close()
        await collector.close()
        await app.market.close()
        await hub.close()
        await db.close()
        await bot.session.close()


if __name__ == "__main__":
    with contextlib.suppress(KeyboardInterrupt):
        asyncio.run(main())
