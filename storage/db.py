"""SQLite storage: users, every collected news item, and the crypto price around each item.

All datetimes are stored as naive UTC (SQLite has no time zones).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
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
    delete,
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
    return datetime.now(UTC).replace(tzinfo=None)


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
    calendar_alerts: Mapped[bool] = mapped_column(Boolean, default=True)  # remind before big releases
    # Daily digest
    digest_on: Mapped[bool] = mapped_column(Boolean, default=True)
    digest_hour: Mapped[int] = mapped_column(Integer, default=8)  # local hour
    tz_offset: Mapped[int] = mapped_column(Integer, default=0)  # minutes from UTC
    last_digest: Mapped[str] = mapped_column(String(10), default="")  # local date of the last digest
    # Convenience
    onboarded: Mapped[bool] = mapped_column(Boolean, default=False)  # finished the first-run setup
    muted_until: Mapped[datetime | None] = mapped_column(DateTime)  # alerts paused until (UTC)
    quiet_start: Mapped[int] = mapped_column(Integer, default=-1)  # local hour, -1 = no quiet hours
    quiet_end: Mapped[int] = mapped_column(Integer, default=-1)
    quiet_critical: Mapped[bool] = mapped_column(Boolean, default=True)  # 🔴 critical alerts still come in quiet hours

    @property
    def categories(self) -> set[str]:
        return {c for c in (self.alert_categories or "").split(",") if c}

    def is_muted(self, now: datetime) -> bool:
        return self.muted_until is not None and self.muted_until > now

    def is_quiet(self, now: datetime) -> bool:
        """Inside the user's quiet hours (local time)?"""
        start, end = self.quiet_start, self.quiet_end
        if start is None or end is None or start < 0 or end < 0 or start == end:
            return False
        hour = self.local(now).hour
        return start <= hour < end if start < end else (hour >= start or hour < end)

    def next_local_hour(self, now: datetime, hour: int) -> datetime:
        """The next moment (UTC) when the user's clock shows `hour`:00."""
        local = self.local(now)
        target = local.replace(hour=hour, minute=0, second=0, microsecond=0)
        if target <= local:
            target += timedelta(days=1)
        return target - timedelta(minutes=self.tz_offset or 0)

    def local(self, ts: datetime) -> datetime:
        """UTC -> the user's local time."""
        return ts + timedelta(minutes=self.tz_offset or 0)

    @property
    def tz_label(self) -> str:
        m = self.tz_offset or 0
        sign = "+" if m >= 0 else "−"
        hours, minutes = divmod(abs(m), 60)
        return f"UTC{sign}{hours}" + (f":{minutes:02d}" if minutes else "")


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
    # Tone for crypto: -2 bearish … +2 bullish (see analysis/sentiment.py)
    sentiment: Mapped[int | None] = mapped_column(Integer)
    # live = collected in real time · history = imported from the news archive · calendar = economic release
    origin: Mapped[str] = mapped_column(String(10), default="live")


class SentAlert(Base):
    __tablename__ = "sent_alerts"
    __table_args__ = (UniqueConstraint("user_id", "news_id"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    news_id: Mapped[int] = mapped_column(ForeignKey("news_items.id", ondelete="CASCADE"))
    sent_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, index=True)


class CalendarEventRow(Base):
    """A scheduled economic release (CPI, payrolls, FOMC…)."""

    __tablename__ = "calendar_events"
    __table_args__ = (UniqueConstraint("title", "country", "starts_at"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    title: Mapped[str] = mapped_column(String(150))
    country: Mapped[str] = mapped_column(String(5))
    starts_at: Mapped[datetime] = mapped_column(DateTime, index=True)
    impact: Mapped[int] = mapped_column(Integer)  # 0..3
    forecast: Mapped[str] = mapped_column(String(20), default="")
    previous: Mapped[str] = mapped_column(String(20), default="")
    recorded: Mapped[bool] = mapped_column(Boolean, default=False)  # turned into a memory event after release


class SentReminder(Base):
    __tablename__ = "sent_reminders"
    __table_args__ = (UniqueConstraint("user_id", "event_id"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"))
    event_id: Mapped[int] = mapped_column(ForeignKey("calendar_events.id", ondelete="CASCADE"))
    sent_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class SentimentSnapshot(Base):
    """Hourly snapshot of market mood, so trends can be shown over days."""

    __tablename__ = "sentiment_snapshots"

    id: Mapped[int] = mapped_column(primary_key=True)
    taken_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, index=True)
    mood: Mapped[float | None] = mapped_column(Float)
    fear_greed: Mapped[float | None] = mapped_column(Float)
    news_index: Mapped[float | None] = mapped_column(Float)
    btc_price: Mapped[float | None] = mapped_column(Float)
    funding: Mapped[float | None] = mapped_column(Float)  # average BTC funding, % per 8 h
    long_pct: Mapped[float | None] = mapped_column(Float)
    open_interest: Mapped[float | None] = mapped_column(Float)
    btc_dominance: Mapped[float | None] = mapped_column(Float)
    stablecoins: Mapped[float | None] = mapped_column(Float)


class ForecastRow(Base):
    """Every hourly BTC forecast, scored once its horizon has passed (the live track record)."""

    __tablename__ = "forecasts"

    id: Mapped[int] = mapped_column(primary_key=True)
    made_at: Mapped[datetime] = mapped_column(DateTime, index=True)
    horizon: Mapped[int] = mapped_column(Integer)  # hours
    prob_up: Mapped[float] = mapped_column(Float)
    price: Mapped[float] = mapped_column(Float)
    reliable: Mapped[bool] = mapped_column(Boolean, default=False)  # did the model pass its own test?
    outcome_price: Mapped[float | None] = mapped_column(Float)
    correct: Mapped[bool | None] = mapped_column(Boolean)


class KeyValue(Base):
    """Small persistent state (e.g. history-import progress)."""

    __tablename__ = "kv"

    key: Mapped[str] = mapped_column(String(100), primary_key=True)
    value: Mapped[str] = mapped_column(Text)


# Columns added after the first release: added to existing databases on startup.
MIGRATIONS = {
    "users": {
        "alerts_on": "BOOLEAN NOT NULL DEFAULT 1",
        "alert_level": "INTEGER NOT NULL DEFAULT 7",
        "alert_categories": "VARCHAR(200) NOT NULL DEFAULT ''",
        "calendar_alerts": "BOOLEAN NOT NULL DEFAULT 1",
        "digest_on": "BOOLEAN NOT NULL DEFAULT 1",
        "digest_hour": "INTEGER NOT NULL DEFAULT 8",
        "tz_offset": "INTEGER NOT NULL DEFAULT 0",
        "last_digest": "VARCHAR(10) NOT NULL DEFAULT ''",
        "onboarded": "BOOLEAN NOT NULL DEFAULT 0",
        "muted_until": "DATETIME",
        "quiet_start": "INTEGER NOT NULL DEFAULT -1",
        "quiet_end": "INTEGER NOT NULL DEFAULT -1",
        "quiet_critical": "BOOLEAN NOT NULL DEFAULT 1",
    },
    "news_items": {
        "importance": "INTEGER",
        "importance_reasons": "VARCHAR(300)",
        "sentiment": "INTEGER",
        "origin": "VARCHAR(10) NOT NULL DEFAULT 'live'",
    },
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
        self.path = path
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
            await conn.execute(text("CREATE INDEX IF NOT EXISTS ix_price_marks_asset_minutes ON price_marks (asset, minutes)"))
            await conn.execute(text("PRAGMA journal_mode=WAL"))  # readers don't block the collectors

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

    async def add_news(self, articles: list[Article], *, backfill: bool = False, origin: str = "live") -> list[NewsItem]:
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
                    origin=origin,
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

    async def missing_marks(self, *, max_age_days: int = 120, limit: int = 20000) -> list[tuple[int, str, int, datetime]]:
        """(news_id, asset, minutes, timestamp) for price points that are due but not recorded yet.

        Two groups are checked separately so neither can starve the other:
        news from the last 24 h (some horizons still in the future) and older news with gaps.
        """
        now = utcnow()
        complete = len(HORIZONS) * len(ASSETS)
        settled = now - timedelta(minutes=max(HORIZONS) + 2)
        async with self.session() as s:
            recent = (
                await s.execute(select(NewsItem.id, NewsItem.event_at).where(NewsItem.event_at > settled))
            ).all()
            older = (
                await s.execute(
                    select(NewsItem.id, NewsItem.event_at)
                    .outerjoin(PriceMark, PriceMark.news_id == NewsItem.id)
                    .where(NewsItem.event_at <= settled, NewsItem.event_at >= now - timedelta(days=max_age_days))
                    .group_by(NewsItem.id, NewsItem.event_at)
                    .having(func.count(PriceMark.id) < complete)
                    .order_by(NewsItem.event_at.desc())
                    .limit(4000)
                )
            ).all()
            items = list(recent) + list(older)
            have: set[tuple[int, str, int]] = set()
            ids = [i for i, _ in items]
            for start in range(0, len(ids), 900):  # stay far below SQLite's parameter limit
                rows = await s.execute(
                    select(PriceMark.news_id, PriceMark.asset, PriceMark.minutes).where(
                        PriceMark.news_id.in_(ids[start : start + 900])
                    )
                )
                have.update((n, a, m) for n, a, m in rows)

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

    async def all_reactions(self, *, asset: str = "BTC", days: int = 120) -> list[Reaction]:
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

    async def set_importance(self, scores: dict[int, tuple[int, str, int]]) -> None:
        """{news_id: (importance, reasons, tone)}"""
        async with self.session() as s:
            for news_id, (value, reasons, tone) in scores.items():
                await s.execute(
                    update(NewsItem)
                    .where(NewsItem.id == news_id)
                    .values(importance=value, importance_reasons=reasons[:300], sentiment=tone)
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

    # --- economic calendar --------------------------------------------------------

    async def upsert_calendar(self, events: list) -> int:  # list[collectors.calendar.CalendarEvent]
        """Insert new events, update forecasts of known ones. Returns how many were new."""
        new = 0
        async with self.session() as s:
            for e in events:
                row = await s.scalar(
                    select(CalendarEventRow).where(
                        CalendarEventRow.title == e.title,
                        CalendarEventRow.country == e.country,
                        CalendarEventRow.starts_at == e.starts_at,
                    )
                )
                if row is None:
                    s.add(
                        CalendarEventRow(
                            title=e.title, country=e.country, starts_at=e.starts_at, impact=e.impact,
                            forecast=e.forecast, previous=e.previous,
                        )
                    )
                    new += 1
                else:
                    row.impact, row.forecast, row.previous = e.impact, e.forecast, e.previous
            await s.commit()
        return new

    async def calendar_between(self, start: datetime, end: datetime, min_impact: int = 2) -> list[CalendarEventRow]:
        async with self.session() as s:
            stmt = (
                select(CalendarEventRow)
                .where(
                    CalendarEventRow.starts_at >= start,
                    CalendarEventRow.starts_at < end,
                    CalendarEventRow.impact >= min_impact,
                )
                .order_by(CalendarEventRow.starts_at, CalendarEventRow.impact.desc())
            )
            return list((await s.scalars(stmt)).all())

    async def unrecorded_releases(self, min_impact: int = 3) -> list[CalendarEventRow]:
        """Big releases that already happened but are not yet in the price memory."""
        now = utcnow()
        async with self.session() as s:
            stmt = select(CalendarEventRow).where(
                CalendarEventRow.starts_at <= now,
                CalendarEventRow.starts_at >= now - timedelta(days=7),
                CalendarEventRow.impact >= min_impact,
                CalendarEventRow.recorded.is_(False),
            )
            return list((await s.scalars(stmt)).all())

    async def mark_recorded(self, ids: list[int]) -> None:
        if ids:
            async with self.session() as s:
                await s.execute(update(CalendarEventRow).where(CalendarEventRow.id.in_(ids)).values(recorded=True))
                await s.commit()

    async def was_reminded(self, user_id: int, event_id: int) -> bool:
        async with self.session() as s:
            stmt = select(func.count()).select_from(SentReminder).where(
                SentReminder.user_id == user_id, SentReminder.event_id == event_id
            )
            return bool(await s.scalar(stmt))

    async def record_reminder(self, user_id: int, event_id: int) -> None:
        async with self.session() as s:
            s.add(SentReminder(user_id=user_id, event_id=event_id))
            await s.commit()

    # --- sentiment history -------------------------------------------------------

    async def add_snapshot(self, **values: float | None) -> None:
        async with self.session() as s:
            s.add(SentimentSnapshot(**values))
            await s.commit()

    async def snapshots(self, days: int = 7) -> list[SentimentSnapshot]:
        async with self.session() as s:
            stmt = (
                select(SentimentSnapshot)
                .where(SentimentSnapshot.taken_at >= utcnow() - timedelta(days=days))
                .order_by(SentimentSnapshot.taken_at)
            )
            return list((await s.scalars(stmt)).all())

    # --- small persistent state --------------------------------------------------

    async def kv_get(self, key: str) -> str | None:
        async with self.session() as s:
            row = await s.get(KeyValue, key)
            return row.value if row else None

    async def kv_set(self, key: str, value: str) -> None:
        async with self.session() as s:
            row = await s.get(KeyValue, key)
            if row is None:
                s.add(KeyValue(key=key, value=value))
            else:
                row.value = value
            await s.commit()

    async def kv_keys(self, prefix: str) -> set[str]:
        async with self.session() as s:
            return set((await s.scalars(select(KeyValue.key).where(KeyValue.key.like(f"{prefix}%")))).all())

    # --- digests & reminders ----------------------------------------------------

    async def digest_subscribers(self) -> list[User]:
        async with self.session() as s:
            return list((await s.scalars(select(User).where(User.digest_on.is_(True)))).all())

    async def calendar_subscribers(self) -> list[User]:
        async with self.session() as s:
            return list((await s.scalars(select(User).where(User.calendar_alerts.is_(True)))).all())

    # --- maintenance & admin -------------------------------------------------------

    async def delete_older_than(self, days: int) -> int:
        """Remove news (with its price marks and alert records) older than `days`. Returns news rows removed."""
        cutoff = utcnow() - timedelta(days=days)
        removed = 0
        async with self.session() as s:
            while True:
                ids = list((await s.scalars(select(NewsItem.id).where(NewsItem.event_at < cutoff).limit(2000))).all())
                if not ids:
                    break
                await s.execute(delete(PriceMark).where(PriceMark.news_id.in_(ids)))
                await s.execute(delete(SentAlert).where(SentAlert.news_id.in_(ids)))
                await s.execute(delete(NewsItem).where(NewsItem.id.in_(ids)))
                await s.commit()
                removed += len(ids)
            await s.execute(delete(SentimentSnapshot).where(SentimentSnapshot.taken_at < cutoff))
            old_events = select(CalendarEventRow.id).where(CalendarEventRow.starts_at < cutoff)
            await s.execute(delete(SentReminder).where(SentReminder.event_id.in_(old_events)))
            await s.execute(delete(CalendarEventRow).where(CalendarEventRow.starts_at < cutoff))
            await s.commit()
        return removed

    async def stats(self) -> dict[str, float]:
        day = utcnow() - timedelta(hours=24)
        week = utcnow() - timedelta(days=7)
        queries = {
            "users": select(func.count()).select_from(User),
            "users_alerts": select(func.count()).select_from(User).where(User.alerts_on.is_(True)),
            "users_digest": select(func.count()).select_from(User).where(User.digest_on.is_(True)),
            "users_active_7d": select(func.count()).select_from(User).where(User.last_seen_at >= week),
            "news_total": select(func.count()).select_from(NewsItem),
            "news_24h": select(func.count()).select_from(NewsItem).where(NewsItem.seen_at >= day, NewsItem.origin == "live"),
            "news_history": select(func.count()).select_from(NewsItem).where(NewsItem.origin == "history"),
            "news_unscored": select(func.count()).select_from(NewsItem).where(NewsItem.importance.is_(None)),
            "price_marks": select(func.count()).select_from(PriceMark),
            "alerts_24h": select(func.count()).select_from(SentAlert).where(SentAlert.sent_at >= day),
            "reminders_24h": select(func.count()).select_from(SentReminder).where(SentReminder.sent_at >= day),
            "calendar_events": select(func.count()).select_from(CalendarEventRow),
            "snapshots": select(func.count()).select_from(SentimentSnapshot),
        }
        out: dict[str, float] = {}
        async with self.session() as s:
            for name, stmt in queries.items():
                out[name] = await s.scalar(stmt) or 0
        if self.path != ":memory:":
            db_file = Path(self.path)
            files = list(db_file.parent.glob(db_file.name + "*"))  # .db plus -wal / -shm
            out["db_mb"] = round(sum(f.stat().st_size for f in files) / 1e6, 1)
        return out

    # --- forecast track record ---------------------------------------------------------

    async def add_forecast(self, made_at: datetime, horizon: int, prob_up: float, price: float, reliable: bool) -> None:
        async with self.session() as s:
            s.add(ForecastRow(made_at=made_at, horizon=horizon, prob_up=prob_up, price=price, reliable=reliable))
            await s.commit()

    async def due_forecasts(self) -> list[ForecastRow]:
        """Forecasts whose horizon has passed but that are not scored yet."""
        now = utcnow()
        async with self.session() as s:
            rows = (await s.scalars(select(ForecastRow).where(ForecastRow.outcome_price.is_(None)))).all()
        return [r for r in rows if r.made_at + timedelta(hours=r.horizon, minutes=2) <= now]

    async def resolve_forecast(self, forecast_id: int, outcome_price: float) -> None:
        async with self.session() as s:
            row = await s.get(ForecastRow, forecast_id)
            if row is None:
                return
            row.outcome_price = outcome_price
            if row.prob_up != 0.5:
                row.correct = (outcome_price > row.price) == (row.prob_up > 0.5)
            await s.commit()

    async def forecast_record(self, horizon: int, days: int = 30) -> tuple[int, int, int, int]:
        """(scored, correct, scored with a clear lean ≥ 55 % / ≤ 45 %, correct among those)."""
        async with self.session() as s:
            rows = (
                await s.scalars(
                    select(ForecastRow).where(
                        ForecastRow.horizon == horizon,
                        ForecastRow.correct.is_not(None),
                        ForecastRow.made_at >= utcnow() - timedelta(days=days),
                    )
                )
            ).all()
        clear = [r for r in rows if abs(r.prob_up - 0.5) >= 0.05]
        return len(rows), sum(1 for r in rows if r.correct), len(clear), sum(1 for r in clear if r.correct)

    async def first_forecast_at(self) -> datetime | None:
        async with self.session() as s:
            return await s.scalar(select(func.min(ForecastRow.made_at)))
