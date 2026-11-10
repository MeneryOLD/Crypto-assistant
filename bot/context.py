"""Shared services (one object passed to handlers and jobs) and the full market snapshot they read."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from analysis.patterns import PatternCache
from analysis.sentiment import Mood, NewsSentiment, market_mood, news_sentiment
from analysis.similar import SimilarityEngine
from collectors.exchanges import ExchangeHub, Ticker
from collectors.fear_greed import FearGreed, FearGreedClient
from collectors.market_data import GlobalMarket, Liquidity, MarketDataClient, Network, Positioning
from collectors.news import NewsCollector
from config import Settings
from storage.db import Database

if TYPE_CHECKING:
    from bot.notifier import Notifier
    from bot.scheduler import Scheduler


@dataclass
class App:
    settings: Settings
    db: Database
    hub: ExchangeHub
    collector: NewsCollector
    fg: FearGreedClient
    market: MarketDataClient
    similar: SimilarityEngine
    patterns: PatternCache
    scheduler: Scheduler | None = None
    notifier: Notifier | None = None

    def is_admin(self, telegram_id: int) -> bool:
        return telegram_id in self.settings.admin_list


@dataclass
class MarketView:
    """Everything the sentiment screen, the daily digest and the hourly snapshot need."""

    tickers: dict[str, Ticker] = field(default_factory=dict)
    change_7d: dict[str, float] = field(default_factory=dict)
    fear_greed: FearGreed | None = None
    global_market: GlobalMarket | None = None
    positioning: Positioning | None = None
    liquidity: Liquidity | None = None
    network: Network | None = None
    funding: dict[str, float] = field(default_factory=dict)  # exchange -> rate (fraction per 8 h)
    news: NewsSentiment | None = None
    news_by_category: dict[str, NewsSentiment] = field(default_factory=dict)
    mood: Mood | None = None

    @property
    def funding_avg_pct(self) -> float | None:
        return sum(self.funding.values()) / len(self.funding) * 100 if self.funding else None


VIEW_COINS = ("BTC", "ETH", "SOL")


async def market_view(app: App, *, news_hours: int = 24) -> MarketView:
    ex = app.hub.names[0]

    async def safe(coro):  # type: ignore[no-untyped-def]
        try:
            return await coro
        except Exception:
            return None

    tickers = await asyncio.gather(*(safe(app.hub.ticker(ex, c)) for c in VIEW_COINS))
    weekly = await asyncio.gather(*(safe(app.hub.change_7d(ex, c)) for c in VIEW_COINS))
    fg, glob, pos, liq, net, funding, items = await asyncio.gather(
        safe(app.fg.get()),
        safe(app.market.global_market()),
        safe(app.market.positioning()),
        safe(app.market.liquidity()),
        safe(app.market.network()),
        safe(app.hub.funding_rates("BTC")),
        app.db.recent_news(limit=6000, hours=news_hours),
    )
    view = MarketView(
        tickers={c: t for c, t in zip(VIEW_COINS, tickers, strict=True) if isinstance(t, Ticker)},
        change_7d={c: w for c, w in zip(VIEW_COINS, weekly, strict=True) if w is not None},
        fear_greed=fg,
        global_market=glob,
        positioning=pos,
        liquidity=liq,
        network=net,
        funding=funding or {},
    )
    live = [n for n in items if n.origin == "live"]
    view.news = news_sentiment(live)
    by_cat: dict[str, list] = {}
    for n in live:
        by_cat.setdefault(n.category, []).append(n)
    view.news_by_category = {c: news_sentiment(v) for c, v in by_cat.items()}
    btc = view.tickers.get("BTC")
    view.mood = market_mood(
        fear_greed=fg.value if fg else None,
        news_index=view.news.index if view.news and (view.news.bullish + view.news.bearish) >= 10 else None,
        btc_change_24h=btc.change_pct if btc else None,
        btc_change_7d=view.change_7d.get("BTC"),
        funding_avg_pct=view.funding_avg_pct,
        long_pct=pos.long_pct if pos else None,
    )
    return view
