"""Background jobs: market snapshots + price alerts, news collection + news alerts."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable

from aiogram import Bot
from aiogram.exceptions import TelegramAPIError, TelegramForbiddenError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app import services
from app.config import Settings
from app.formatting import fmt_pct, fmt_price, h
from app.market.coingecko import CoinGeckoClient, MarketDataError
from app.market.service import store_snapshots
from app.models import AlertKind
from app.news.collector import collect_news

log = logging.getLogger(__name__)


async def _send(bot: Bot, chat_id: int, text: str) -> None:
    try:
        await bot.send_message(chat_id, text, disable_web_page_preview=True)
    except TelegramForbiddenError:
        log.info("User %s blocked the bot; skipping", chat_id)
    except TelegramAPIError as exc:
        log.warning("Failed to send to %s: %s", chat_id, exc)
    await asyncio.sleep(0.05)  # stay well under Telegram's broadcast limits


async def market_job(bot: Bot, sf: async_sessionmaker[AsyncSession], cg: CoinGeckoClient, settings: Settings) -> None:
    top = await cg.top_markets(settings.tracked_coins_count)
    async with sf() as session:
        await store_snapshots(session, top)
        markets = {m.coin_id: m for m in top}
        missing = [c for c in await services.active_alert_coin_ids(session) if c not in markets]
        if missing:
            markets.update({m.coin_id: m for m in await cg.markets_for(missing)})
        triggered = await services.evaluate_alerts(session, markets)

    for t in triggered:
        a, m = t.alert, t.market
        if a.kind is AlertKind.CHANGE:
            detail = f"24h change {fmt_pct(m.change_24h)} (threshold ±{a.threshold:g}%)"
        else:
            detail = f"price {fmt_price(m.price_usd)} is {a.kind.value} {fmt_price(a.threshold)}"
        await _send(bot, t.chat_id, f"🔔 <b>Alert #{a.id}: {h(m.name)} ({h(m.symbol)})</b>\n{detail}")
    log.info("market job: %d snapshots, %d alerts triggered", len(top), len(triggered))


async def news_job(bot: Bot, sf: async_sessionmaker[AsyncSession], settings: Settings) -> None:
    async with sf() as session:
        new_items = await collect_news(session, settings.feeds)
        notifications = await services.match_news_to_watchlists(session, new_items)
    for n in notifications:
        coins = ", ".join(h(c) for c in n.coins)
        await _send(
            bot,
            n.chat_id,
            f"📰 <b>{coins}</b> in the news\n<a href=\"{h(n.item.url)}\">{h(n.item.title)}</a>\n<i>{h(n.item.source)}</i>",
        )
    log.info("news job: %d new articles, %d notifications", len(new_items), len(notifications))


async def run_periodically(name: str, interval: float, job: Callable[[], Awaitable[None]], *, first_delay: float = 0) -> None:
    await asyncio.sleep(first_delay)
    while True:
        try:
            await job()
        except MarketDataError as exc:
            log.warning("%s: market data unavailable: %s", name, exc)
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("%s failed", name)
        await asyncio.sleep(interval)
