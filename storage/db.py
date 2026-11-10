"""SQLite storage: users, every collected news item, and the crypto price around each item.

All datetimes are stored as naive UTC (SQLite has no time zones).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

from sqlalchemy import (
    BigInteger,
    Boolean,
    DateTime,
    Float,
    ForeignKey,
    Integer,
    String,
    Text,
    UniqueConstraint,
    func,
    or_,
    select,
    text,
    update,
)
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

from collectors.news import Article, title_fingerprint

HORIZONS = (0, 15, 60, 240, 1440)  # minutes after the news: at publication, +15m, +1h, +4h, +24h
ASSETS = ("BTC", "ETH")


def utcnow() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


class Base(DeclarativeBase):
    pass


class User(Base):
    __tablename__ = "users"

    id: Mapped[int] = mapped_column(primary_key=True)
    telegram_id: Mapped[int] = mapped_column(BigInteger, unique=True)
    username: Mapped[str | None] = mapped_column(String(64))
    first_name: Mapped[str | None] = mapped_column(String(128))
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    last_seen_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    # Notification settings
    alerts_on: Mapped[bool] = mapped_column(Boolean, default=True)
    alert_level: Mapped[int] = mapped_column(Integer, default=7)  # minimum importance to notify
    alert_categories: Mapped[str] = mapped_column(String(200), default="")  # comma list, "" = all

    @property
    def categories(self) -> set[str]:
        return {c for c in (self.alert_categories or "").split(",") if c}


class NewsItem(Base):
    __tablename__ = "news_items"

    id: Mapped[int] = mapped_column(primary_key=True)
    source: Mapped[str] = mapped_column(String(100))
    category: Mapped[str] = mapped_column(String(20), index=True)
    title: Mapped[str] = mapped_column(String(500))
    url: Mapped[str] = mapped_column(String(1000), unique=True)
    title_hash: Mapped[str] = mapped_column(String(64), unique=True)
    summary: Mapped[str | None] = mapped_column(Text)
    published_at: Mapped[datetime | None] = mapped_column(DateTime)
    seen_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    # The moment we measure price reactions from: publication time if trustworthy, else when we saw it.
    event_at: Mapped[datetime] = mapped_column(DateTime, index=True)
    # True for items already in the feeds when the bot first read them (not "breaking" for us).
    backfill: Mapped[bool] = mapped_column(Boolean, default=False)
    # Importance 1–10 and why (see analysis/importance.py). NULL = not scored yet.
    importance: Mapped[int | None] = mapped_column(Integer, index=True)
    importance_reasons: Mapped[str | None] = mapped_column(String(300))


class SentAlert(Base):
    __tablename__ = "sent_alerts"
    __table_args__ = (UniqueConstraint("user_id", "news_id"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    news_id: Mapped[int] = mapped_column(ForeignKey("news_items.id", ondelete="CASCADE"))
    sent_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, index=True)


# Columns added after the first release: added to existing databases on startup.
MIGRATIONS = {
    "users": {
        "alerts_on": "BOOLEAN NOT NULL DEFAULT 1",
        "alert_level": "INTEGER NOT NULL DEFAULT 7",
        "alert_categories": "VARCHAR(200) NOT NULL DEFAULT ''",
    },
    "news_items": {"importance": "INTEGER", "importance_reasons": "VARCHAR(300)"},
}


class PriceMark(Base):
    """Price of an asset N minutes after a news item — the bot's long-term memory."""

    __tablename__ = "price_marks"
    __table_args__ = (UniqueConstraint("news_id", "asset", "minutes"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    news_id: Mapped[int] = mapped_column(ForeignKey("news_items.id", ondelete="CASCADE"), index=True)
    asset: Mapped[str] = mapped_column(String(10))
    minutes: Mapped[int] = mapped_column(Integer)
    price: Mapped[float] = mapped_column(Float)


@dataclass(frozen=True, slots=True)
class Reaction:
    """How BTC moved after a news item (percent vs. the price at publication)."""

    item: NewsItem
    price_0: float
    changes: dict[int, float]  # minutes -> % change


def event_time(published_at: datetime | None, seen_at: datetime) -> datetime:
    # Some feeds put future or missing dates; fall back to when we saw it.
    if published_at is None or published_at > seen_at + timedelta(minutes=5):
        return seen_at
    return published_at


class Database:
    def __init__(self, path: str):
        if path != ":memory:":
            Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.engine = create_async_engine(f"sqlite+aiosqlite:///{path}", connect_args={"timeout": 30})
        self.session = async_sessionmaker(self.engine, expire_on_commit=False)

    async def init(self) -> None:
        """Create tables that don't exist yet and add any new columns to old databases."""
        async with self.engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
            for table, columns in MIGRATIONS.items():
                existing = {row[1] for row in await conn.execute(text(f"PRAGMA table_info({table})"))}
                for name, ddl in columns.items():
                    if name not in existing:
                        await conn.execute(text(f"ALTER TABLE {table} ADD COLUMN {name} {ddl}"))
            await conn.execute(text("CREATE INDEX IF NOT EXISTS ix_news_items_importance ON news_items (importance)"))

    async def close(self) -> None:
        await self.engine.dispose()

    # --- users -------------------------------------------------------------

    async def upsert_user(self, telegram_id: int, username: str | None, first_name: str | None) -> tuple[User, bool]:
        """Save or update a Telegram user. Returns (user, created)."""
        async with self.session() as s:
            user = await s.scalar(select(User).where(User.telegram_id == telegram_id))
            created = user is None
            if user is None:
                user = User(telegram_id=telegram_id)
                s.add(user)
            user.username = username
            user.first_name = first_name
            user.last_seen_at = utcnow()
            await s.commit()
            return user, created

    async def count_users(self) -> int:
        async with self.session() as s:
            return await s.scalar(select(func.count()).select_from(User)) or 0

    # --- news --------------------------------------------------------------

    async def add_news(self, articles: list[Article], *, backfill: bool = False) -> list[NewsItem]:
        """Store articles not seen before (by URL or by identical headline). Returns the new ones."""
        batch: dict[str, Article] = {}
        urls: set[str] = set()
        for a in articles:
            fp = title_fingerprint(a.title)
            if fp not in batch and a.url not in urls:
                batch[fp] = a
                urls.add(a.url)
        if not batch:
            return []

        async with self.session() as s:
            known = await s.execute(
                select(NewsItem.title_hash, NewsItem.url).where(
                    or_(NewsItem.title_hash.in_(list(batch)), NewsItem.url.in_(list(urls)))
                )
            )
            seen_fps, seen_urls = set(), set()
            for fp, url in known:
                seen_fps.add(fp)
                seen_urls.add(url)

            now = utcnow()
            new = []
            for fp, a in batch.items():
                if fp in seen_fps or a.url in seen_urls:
                    continue
                item = NewsItem(
                    source=a.source,
                    category=a.category,
                    title=a.title,
                    url=a.url,
                    title_hash=fp,
                    summary=a.summary,
                    published_at=a.published_at,
                    seen_at=now,
                    event_at=event_time(a.published_at, now),
                    backfill=backfill,
                )
                s.add(item)
                new.append(item)
            await s.commit()
            return new

    @staticmethod
    def _news_filter(stmt, category: str | None, query: str | None, hours: int):  # type: ignore[no-untyped-def]
        stmt = stmt.where(NewsItem.event_at >= utcnow() - timedelta(hours=hours))
        if category:
            stmt = stmt.where(NewsItem.category == category)
        if query:
            like = f"%{query}%"
            stmt = stmt.where(or_(NewsItem.title.ilike(like), NewsItem.summary.ilike(like)))
        return stmt

    async def recent_news(
        self,
        *,
        limit: int = 10,
        offset: int = 0,
        category: str | None = None,
        query: str | None = None,
        hours: int = 48,
    ) -> list[NewsItem]:
        stmt = self._news_filter(select(NewsItem), category, query, hours)
        stmt = stmt.order_by(NewsItem.event_at.desc()).offset(offset).limit(limit)
        async with self.session() as s:
            return list((await s.scalars(stmt)).all())

    async def count_news(self, *, category: str | None = None, query: str | None = None, hours: int = 48) -> int:
        stmt = self._news_filter(select(func.count()).select_from(NewsItem), category, query, hours)
        async with self.session() as s:
            return await s.scalar(stmt) or 0

    async def count_price_marks(self) -> int:
        async with self.session() as s:
            return await s.scalar(select(func.count()).select_from(PriceMark)) or 0

    async def news_stats(self) -> dict[str, int]:
        async with self.session() as s:
            rows = await s.execute(select(NewsItem.category, func.count()).group_by(NewsItem.category))
            return {c: n for c, n in rows}

    # --- price memory --------------------------------------------------------

    async def missing_marks(self, *, max_age_days: int = 30, limit: int = 3000) -> list[tuple[int, str, int, datetime]]:
        """(news_id, asset, minutes, timestamp) for price points that are due but not recorded yet."""
        now = utcnow()
        complete = len(HORIZONS) * len(ASSETS)
        async with self.session() as s:
            items = (
                await s.execute(
                    select(NewsItem.id, NewsItem.event_at)
                    .outerjoin(PriceMark, PriceMark.news_id == NewsItem.id)
                    .where(NewsItem.event_at >= now - timedelta(days=max_age_days))
                    .group_by(NewsItem.id, NewsItem.event_at)
                    .having(func.count(PriceMark.id) < complete)
                    .order_by(NewsItem.event_at.desc())
                    .limit(500)
                )
            ).all()
            have = {
                (n, a, m)
                for n, a, m in await s.execute(
                    select(PriceMark.news_id, PriceMark.asset, PriceMark.minutes).where(
                        PriceMark.news_id.in_([i for i, _ in items])
                    )
                )
            } if items else set()

        due = []
        for news_id, event_at in items:
            for minutes in HORIZONS:
                ts = event_at + timedelta(minutes=minutes)
                if ts > now - timedelta(minutes=2):  # that minute's candle isn't final yet
                    continue
                for asset in ASSETS:
                    if (news_id, asset, minutes) not in have:
                        due.append((news_id, asset, minutes, ts))
                        if len(due) >= limit:
                            return due
        return due

    async def add_marks(self, marks: list[tuple[int, str, int, float]]) -> None:
        if not marks:
            return
        async with self.session() as s:
            s.add_all(PriceMark(news_id=n, asset=a, minutes=m, price=p) for n, a, m, p in marks)
            await s.commit()

    async def reactions(
        self, *, asset: str = "BTC", category: str | None = None, days: int = 7, horizon: int = 60, limit: int = 10
    ) -> list[Reaction]:
        """News items followed by the biggest price moves at `horizon` minutes."""
        since = utcnow() - timedelta(days=days)
        async with self.session() as s:
            stmt = select(NewsItem).where(NewsItem.event_at >= since)
            if category:
                stmt = stmt.where(NewsItem.category == category)
            items = {i.id: i for i in (await s.scalars(stmt)).all()}
            if not items:
                return []
            marks: dict[int, dict[int, float]] = {}
            for news_id, minutes, price in await s.execute(
                select(PriceMark.news_id, PriceMark.minutes, PriceMark.price).where(
                    PriceMark.asset == asset, PriceMark.news_id.in_(list(items))
                )
            ):
                marks.setdefault(news_id, {})[minutes] = price

        result = []
        for news_id, prices in marks.items():
            base = prices.get(0)
            if not base or horizon not in prices:
                continue
            changes = {m: (p - base) / base * 100 for m, p in prices.items() if m}
            result.append(Reaction(items[news_id], base, changes))
        result.sort(key=lambda r: abs(r.changes[horizon]), reverse=True)
        return result[:limit]

    # --- article details & patterns -------------------------------------------

    async def get_news(self, news_id: int) -> NewsItem | None:
        async with self.session() as s:
            return await s.get(NewsItem, news_id)

    async def marks_for(self, news_id: int) -> dict[tuple[str, int], float]:
        """{(asset, minutes): price} recorded for one news item."""
        async with self.session() as s:
            rows = await s.execute(
                select(PriceMark.asset, PriceMark.minutes, PriceMark.price).where(PriceMark.news_id == news_id)
            )
            return {(a, m): p for a, m, p in rows}

    async def news_between(self, start: datetime, end: datetime) -> list[NewsItem]:
        async with self.session() as s:
            stmt = select(NewsItem).where(NewsItem.event_at >= start, NewsItem.event_at <= end)
            return list((await s.scalars(stmt)).all())

    async def all_reactions(self, *, asset: str = "BTC", days: int = 30) -> list[Reaction]:
        """Every news item (last `days`) that has a price at publication, with all known moves."""
        since = utcnow() - timedelta(days=days)
        async with self.session() as s:
            items = {i.id: i for i in (await s.scalars(select(NewsItem).where(NewsItem.event_at >= since))).all()}
            if not items:
                return []
            marks: dict[int, dict[int, float]] = {}
            for news_id, minutes, price in await s.execute(
                select(PriceMark.news_id, PriceMark.minutes, PriceMark.price)
                .join(NewsItem, NewsItem.id == PriceMark.news_id)
                .where(PriceMark.asset == asset, NewsItem.event_at >= since)
            ):
                marks.setdefault(news_id, {})[minutes] = price
        result = []
        for news_id, prices in marks.items():
            base = prices.get(0)
            if base:
                result.append(Reaction(items[news_id], base, {m: (p - base) / base * 100 for m, p in prices.items() if m}))
        return result

    # --- importance & notifications -------------------------------------------

    async def unscored_news(self, limit: int = 500) -> list[NewsItem]:
        async with self.session() as s:
            stmt = select(NewsItem).where(NewsItem.importance.is_(None)).order_by(NewsItem.event_at.desc()).limit(limit)
            return list((await s.scalars(stmt)).all())

    async def set_importance(self, scores: dict[int, tuple[int, str]]) -> None:
        async with self.session() as s:
            for news_id, (value, reasons) in scores.items():
                await s.execute(
                    update(NewsItem).where(NewsItem.id == news_id).values(importance=value, importance_reasons=reasons[:300])
                )
            await s.commit()

    async def top_news(self, *, hours: int = 24, category: str | None = None, limit: int = 80) -> list[NewsItem]:
        """Most important news first (newest first among equal scores)."""
        stmt = self._news_filter(select(NewsItem), category, None, hours).where(NewsItem.importance.is_not(None))
        stmt = stmt.order_by(NewsItem.importance.desc(), NewsItem.event_at.desc()).limit(limit)
        async with self.session() as s:
            return list((await s.scalars(stmt)).all())

    async def get_user(self, telegram_id: int) -> User | None:
        async with self.session() as s:
            return await s.scalar(select(User).where(User.telegram_id == telegram_id))

    async def update_user(self, telegram_id: int, **values: object) -> User | None:
        async with self.session() as s:
            await s.execute(update(User).where(User.telegram_id == telegram_id).values(**values))
            await s.commit()
        return await self.get_user(telegram_id)

    async def alert_subscribers(self) -> list[User]:
        async with self.session() as s:
            return list((await s.scalars(select(User).where(User.alerts_on.is_(True)))).all())

    async def recent_alerts(self, user_id: int, hours: int = 6) -> list[NewsItem]:
        """News already sent to this user recently (to avoid repeating the same story)."""
        async with self.session() as s:
            stmt = (
                select(NewsItem)
                .join(SentAlert, SentAlert.news_id == NewsItem.id)
                .where(SentAlert.user_id == user_id, SentAlert.sent_at >= utcnow() - timedelta(hours=hours))
            )
            return list((await s.scalars(stmt)).all())

    async def count_alerts_since(self, user_id: int, since: datetime) -> int:
        async with self.session() as s:
            stmt = select(func.count()).select_from(SentAlert).where(SentAlert.user_id == user_id, SentAlert.sent_at >= since)
            return await s.scalar(stmt) or 0

    async def record_alert(self, user_id: int, news_id: int) -> None:
        async with self.session() as s:
            s.add(SentAlert(user_id=user_id, news_id=news_id))
            await s.commit()

    async def count_matching(self, min_level: int, categories: set[str], hours: int = 24) -> int:
        """How many news items in the last `hours` would pass these alert settings."""
        stmt = (
            select(func.count())
            .select_from(NewsItem)
            .where(NewsItem.event_at >= utcnow() - timedelta(hours=hours), NewsItem.importance >= min_level)
        )
        if categories:
            stmt = stmt.where(NewsItem.category.in_(categories))
        async with self.session() as s:
            return await s.scalar(stmt) or 0
