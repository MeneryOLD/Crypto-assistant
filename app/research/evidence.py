"""Collects the evidence a research question is answered from."""

from __future__ import annotations

import logging
import re

from sqlalchemy.ext.asyncio import AsyncSession

from app.market.coingecko import CoinGeckoClient, CoinMarket, MarketDataError
from app.market.service import latest_snapshots
from app.news.collector import recent_news
from app.research.assistant import Evidence

log = logging.getLogger(__name__)

MAX_NEWS = 30


def coins_mentioned(question: str, markets: list[CoinMarket]) -> list[CoinMarket]:
    q = question.lower()
    found = []
    for m in markets:
        names = [m.name.lower(), m.coin_id.lower()]
        if len(m.symbol) > 2:
            names.append(m.symbol.lower())
        if any(re.search(rf"(?<!\w){re.escape(n)}(?!\w)", q) for n in names):
            found.append(m)
    return found


async def build_evidence(session: AsyncSession, cg: CoinGeckoClient, question: str, tracked: int) -> Evidence:
    global_market = None
    try:
        markets = await cg.top_markets(tracked)
        global_market = await cg.global_market()
    except MarketDataError as exc:
        log.warning("Live market data unavailable, using stored snapshots: %s", exc)
        markets = [
            CoinMarket(
                coin_id=s.coin_id, symbol=s.symbol, name=s.name, price_usd=s.price_usd, rank=s.rank,
                market_cap=s.market_cap, volume_24h=s.volume_24h, change_1h=s.change_1h,
                change_24h=s.change_24h, change_7d=s.change_7d,
            )
            for s in await latest_snapshots(session)
        ]

    mentioned = coins_mentioned(question, markets)
    news = []
    if mentioned:
        terms = [t for m in mentioned for t in (m.name, m.symbol if len(m.symbol) > 2 else "")]
        news = await recent_news(session, limit=MAX_NEWS // 2, hours=72, terms=terms)
    general = await recent_news(session, limit=MAX_NEWS, hours=48)
    seen = {n.id for n in news}
    news += [n for n in general if n.id not in seen][: MAX_NEWS - len(news)]

    return Evidence(markets=markets, news=news, global_market=global_market)
