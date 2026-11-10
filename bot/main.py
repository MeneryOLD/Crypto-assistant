"""Starts the bot and its background jobs.  Run with:  python -m bot.main"""

from __future__ import annotations

import asyncio
import contextlib
import logging

from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.types import BotCommand

from bot.handlers import router
from analysis.patterns import PatternCache
from bot.jobs import NewsJob, PriceMemoryJob, ScoringJob, run_every
from bot.notifier import Notifier
from collectors.exchanges import ExchangeHub
from collectors.fear_greed import FearGreedClient
from collectors.news import NewsCollector
from config import get_settings
from storage.db import Database

COMMANDS = [
    BotCommand(command="start", description="🏠 Home"),
    BotCommand(command="top", description="🔥 Most important news"),
    BotCommand(command="news", description="📰 World news"),
    BotCommand(command="alerts", description="🔔 Notification settings"),
    BotCommand(command="markets", description="💹 Coin prices on exchanges"),
    BotCommand(command="history", description="🧠 How BTC moved after news"),
    BotCommand(command="status", description="📡 Sources & exchanges"),
    BotCommand(command="help", description="❓ How to use"),
]


async def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    logging.getLogger("httpx").setLevel(logging.WARNING)  # don't log every feed request
    settings = get_settings()

    db = Database(settings.db_path)
    await db.init()
    exchanges = settings.exchange_list
    if settings.price_history_exchange not in exchanges:
        exchanges.append(settings.price_history_exchange)
    hub = ExchangeHub(exchanges)
    collector = NewsCollector()

    bot = Bot(
        settings.bot_token,
        default=DefaultBotProperties(parse_mode=ParseMode.HTML, link_preview_is_disabled=True),
    )
    # Objects passed here are available to every handler by parameter name.
    notifier = Notifier(bot, db, hub, PatternCache(db))
    dp = Dispatcher(db=db, hub=hub, collector=collector, fg=FearGreedClient(), notifier=notifier)
    dp.include_router(router)

    jobs = [
        # Fast Telegram channels get their own loop so slow websites never delay them.
        asyncio.create_task(run_every("news-fast", 3, NewsJob(db, collector, {"telegram"}, notifier))),
        asyncio.create_task(run_every("news", 5, NewsJob(db, collector, {"rss", "google"}, notifier))),
        asyncio.create_task(run_every("scoring", 20, ScoringJob(db))),
        asyncio.create_task(run_every("price-memory", 60, PriceMemoryJob(db, hub, settings.price_history_exchange))),
    ]
    try:
        await bot.set_my_commands(COMMANDS)  # the "Menu" button in Telegram
        me = await bot.get_me()
        logging.info(
            "Bot @%s started: %d news sources, %d exchanges. Press Ctrl+C to stop.",
            me.username, len(collector.sources), len(hub.names),
        )
        await dp.start_polling(bot)
    finally:
        for task in jobs:
            task.cancel()
        for task in jobs:
            with contextlib.suppress(asyncio.CancelledError):
                await task
        await collector.close()
        await hub.close()
        await db.close()
        await bot.session.close()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
