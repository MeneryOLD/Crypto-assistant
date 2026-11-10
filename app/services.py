"""User, watchlist and alert business logic (independent of Telegram)."""

from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.market.coingecko import CoinMarket, CoinRef
from app.models import AlertKind, NewsItem, PriceAlert, User, WatchlistItem, utcnow
from app.news.collector import mentions

MAX_WATCHLIST = 30
MAX_ALERTS = 30


class LimitReached(Exception):
    pass


# --- users -----------------------------------------------------------------


async def upsert_user(
    session: AsyncSession,
    *,
    telegram_id: int,
    chat_id: int,
    username: str | None,
    first_name: str | None,
    last_name: str | None,
) -> User:
    user = await session.scalar(select(User).where(User.telegram_id == telegram_id))
    if user is None:
        user = User(telegram_id=telegram_id, chat_id=chat_id)
        session.add(user)
    user.chat_id = chat_id
    user.username = username
    user.first_name = first_name
    user.last_name = last_name
    await session.commit()
    return user


# --- watchlist -------------------------------------------------------------


async def get_watchlist(session: AsyncSession, user: User) -> list[WatchlistItem]:
    stmt = select(WatchlistItem).where(WatchlistItem.user_id == user.id).order_by(WatchlistItem.created_at)
    return list((await session.scalars(stmt)).all())


async def add_to_watchlist(session: AsyncSession, user: User, coin: CoinRef) -> bool:
    """Returns False if the coin was already on the watchlist."""
    items = await get_watchlist(session, user)
    if any(i.coin_id == coin.coin_id for i in items):
        return False
    if len(items) >= MAX_WATCHLIST:
        raise LimitReached(f"Watchlist is limited to {MAX_WATCHLIST} coins.")
    session.add(WatchlistItem(user_id=user.id, coin_id=coin.coin_id, symbol=coin.symbol, name=coin.name))
    await session.commit()
    return True


async def remove_from_watchlist(session: AsyncSession, user: User, coin_id: str) -> bool:
    result = await session.execute(
        delete(WatchlistItem).where(WatchlistItem.user_id == user.id, WatchlistItem.coin_id == coin_id)
    )
    await session.commit()
    return result.rowcount > 0


# --- alerts ----------------------------------------------------------------


async def get_alerts(session: AsyncSession, user: User) -> list[PriceAlert]:
    stmt = (
        select(PriceAlert)
        .where(PriceAlert.user_id == user.id, PriceAlert.active.is_(True))
        .order_by(PriceAlert.created_at)
    )
    return list((await session.scalars(stmt)).all())


async def add_alert(session: AsyncSession, user: User, coin: CoinRef, kind: AlertKind, threshold: float) -> PriceAlert:
    if threshold <= 0:
        raise ValueError("Threshold must be positive.")
    if len(await get_alerts(session, user)) >= MAX_ALERTS:
        raise LimitReached(f"You can have at most {MAX_ALERTS} active alerts.")
    alert = PriceAlert(user_id=user.id, coin_id=coin.coin_id, symbol=coin.symbol, kind=kind, threshold=threshold)
    session.add(alert)
    await session.commit()
    return alert


async def delete_alert(session: AsyncSession, user: User, alert_id: int) -> bool:
    result = await session.execute(delete(PriceAlert).where(PriceAlert.id == alert_id, PriceAlert.user_id == user.id))
    await session.commit()
    return result.rowcount > 0


def alert_is_triggered(alert: PriceAlert, market: CoinMarket) -> bool:
    if alert.kind is AlertKind.ABOVE:
        return market.price_usd >= alert.threshold
    if alert.kind is AlertKind.BELOW:
        return market.price_usd <= alert.threshold
    if alert.kind is AlertKind.CHANGE:
        return market.change_24h is not None and abs(market.change_24h) >= alert.threshold
    return False


async def active_alert_coin_ids(session: AsyncSession) -> list[str]:
    stmt = select(PriceAlert.coin_id).where(PriceAlert.active.is_(True)).distinct()
    return list((await session.scalars(stmt)).all())


@dataclass(slots=True)
class TriggeredAlert:
    chat_id: int
    alert: PriceAlert
    market: CoinMarket


async def evaluate_alerts(session: AsyncSession, markets: dict[str, CoinMarket]) -> list[TriggeredAlert]:
    """Find triggered alerts, deactivate them (one-shot) and return what to send."""
    if not markets:
        return []
    stmt = (
        select(PriceAlert, User.chat_id)
        .join(User, User.id == PriceAlert.user_id)
        .where(PriceAlert.active.is_(True), PriceAlert.coin_id.in_(list(markets)))
    )
    triggered = []
    for alert, chat_id in (await session.execute(stmt)).all():
        market = markets[alert.coin_id]
        if alert_is_triggered(alert, market):
            alert.active = False
            alert.triggered_at = utcnow()
            triggered.append(TriggeredAlert(chat_id=chat_id, alert=alert, market=market))
    await session.commit()
    return triggered


# --- news notifications ----------------------------------------------------


@dataclass(slots=True)
class NewsNotification:
    chat_id: int
    item: NewsItem
    coins: list[str]


async def match_news_to_watchlists(session: AsyncSession, items: list[NewsItem]) -> list[NewsNotification]:
    """Match unnotified news to users whose watchlist coins are mentioned, then mark them notified."""
    if not items:
        return []
    rows = (
        await session.execute(
            select(User.chat_id, WatchlistItem.symbol, WatchlistItem.name)
            .join(WatchlistItem, WatchlistItem.user_id == User.id)
            .where(User.news_alerts.is_(True))
        )
    ).all()
    by_chat: dict[int, list[tuple[str, str]]] = {}
    for chat_id, symbol, name in rows:
        by_chat.setdefault(chat_id, []).append((symbol, name))

    notifications = []
    for item in items:
        for chat_id, coins in by_chat.items():
            hits = [symbol for symbol, name in coins if mentions(item, [name, symbol] if len(symbol) > 2 else [name])]
            if hits:
                notifications.append(NewsNotification(chat_id=chat_id, item=item, coins=hits))
        item.notified = True
    await session.commit()
    return notifications
