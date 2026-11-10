"""Entry point: python -m app"""

from __future__ import annotations

import asyncio
import contextlib
import logging

from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.types import BotCommand

from app.bot.handlers import router
from app.bot.middleware import DbUserMiddleware
from app.config import get_settings
from app.db import create_engine, create_session_factory, init_db
from app.jobs import market_job, news_job, run_periodically
from app.market.coingecko import CoinGeckoClient, CoinResolver
from app.research.assistant import ResearchAssistant

log = logging.getLogger("app")

COMMANDS = [
    BotCommand(command="start", description="Start the bot"),
    BotCommand(command="help", description="Available commands"),
    BotCommand(command="market", description="Market overview"),
    BotCommand(command="price", description="Check prices, e.g. /price btc eth"),
    BotCommand(command="news", description="Latest relevant news"),
    BotCommand(command="research", description="AI-powered research"),
    BotCommand(command="watchlist", description="Manage followed assets"),
    BotCommand(command="alerts", description="Manage price alerts"),
]


async def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    settings = get_settings()

    engine = create_engine(settings.database_url)
    await init_db(engine)
    session_factory = create_session_factory(engine)

    cg = CoinGeckoClient(settings.coingecko_api_key)
    resolver = CoinResolver(cg, settings.tracked_coins_count)
    assistant = ResearchAssistant(settings.anthropic_api_key, model=settings.research_model, effort=settings.research_effort)
    if not assistant.enabled:
        log.warning("ANTHROPIC_API_KEY is not set; /research is disabled")

    bot = Bot(settings.bot_token, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
    dp = Dispatcher(settings=settings, cg=cg, resolver=resolver, assistant=assistant)
    middleware = DbUserMiddleware(session_factory)
    dp.message.middleware(middleware)
    dp.callback_query.middleware(middleware)
    dp.include_router(router)

    await bot.set_my_commands(COMMANDS)

    jobs = [
        asyncio.create_task(
            run_periodically("market", settings.market_poll_seconds, lambda: market_job(bot, session_factory, cg, settings))
        ),
        asyncio.create_task(
            run_periodically("news", settings.news_poll_seconds, lambda: news_job(bot, session_factory, settings), first_delay=5)
        ),
    ]
    try:
        me = await bot.get_me()
        log.info("Bot @%s is running", me.username)
        await dp.start_polling(bot)
    finally:
        for task in jobs:
            task.cancel()
        for task in jobs:
            with contextlib.suppress(asyncio.CancelledError):
                await task
        await cg.close()
        await bot.session.close()
        await engine.dispose()


def run() -> None:
    with contextlib.suppress(KeyboardInterrupt):
        asyncio.run(main())


if __name__ == "__main__":
    run()
