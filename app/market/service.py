"""Market data collection and storage."""

from __future__ import annotations

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.market.coingecko import CoinMarket
from app.models import MarketSnapshot


async def store_snapshots(session: AsyncSession, markets: list[CoinMarket]) -> int:
    for m in markets:
        session.add(
            MarketSnapshot(
                coin_id=m.coin_id,
                symbol=m.symbol,
                name=m.name,
                rank=m.rank,
                price_usd=m.price_usd,
                market_cap=m.market_cap,
                volume_24h=m.volume_24h,
                change_1h=m.change_1h,
                change_24h=m.change_24h,
                change_7d=m.change_7d,
            )
        )
    await session.commit()
    return len(markets)


async def latest_snapshots(session: AsyncSession) -> list[MarketSnapshot]:
    """Most recent snapshot per coin, ordered by market-cap rank."""
    latest = (
        select(MarketSnapshot.coin_id, func.max(MarketSnapshot.captured_at).label("ts"))
        .group_by(MarketSnapshot.coin_id)
        .subquery()
    )
    stmt = (
        select(MarketSnapshot)
        .join(latest, (MarketSnapshot.coin_id == latest.c.coin_id) & (MarketSnapshot.captured_at == latest.c.ts))
        .order_by(MarketSnapshot.rank.is_(None), MarketSnapshot.rank)
    )
    return list((await session.scalars(stmt)).all())
