"""Background jobs. Each is a small callable object run by bot.scheduler.Scheduler."""

from __future__ import annotations

import asyncio
import logging
import time
from datetime import timedelta

import httpx
from aiogram import Bot
from aiogram.exceptions import TelegramAPIError, TelegramForbiddenError

from analysis.reactions import fill_price_marks, minute_ms, prices_at
from bot import insights
from bot.context import App, market_view
from bot.notifier import Notifier, score_items
from collectors import calendar as econ_calendar
from collectors import history
from collectors.calendar import FLAGS
from collectors.news import Article, NewsCollector
from storage.db import Database, utcnow

log = logging.getLogger(__name__)


class NewsJob:
    """Checks the sources whose interval has passed, scores what's new and sends alerts."""

    def __init__(self, db: Database, collector: NewsCollector, kinds: set[str] | None = None, notifier: Notifier | None = None):
        self.db = db
        self.collector = collector
        self.kinds = kinds
        self.notifier = notifier
        self.first_run = True

    async def __call__(self) -> None:
        articles = await self.collector.poll(only_due=True, kinds=self.kinds)
        # Whatever is already in the feeds at startup is history, not breaking news.
        new = await self.db.add_news(articles, backfill=self.first_run)
        via = {a.url: a.via for a in articles}
        for item in new:
            if st := self.collector.status.get(via.get(item.url, "")):
                st.new_total += 1
        if new and not self.first_run:
            await score_items(self.db, new)
            for item in new:
                log.info("  NEW %s/10 [%s] %s: %s", item.importance, item.category, item.source, item.title[:110])
            if self.notifier:
                await self.notifier.notify(new)
        elif new:
            log.info("news: %d items loaded at startup (%s)", len(new), ",".join(sorted(self.kinds or [])))
        self.first_run = False


class ScoringJob:
    """Scores items that have no importance yet (startup backfill, archive import, calendar events)."""

    def __init__(self, db: Database):
        self.db = db

    async def __call__(self) -> None:
        items = await self.db.unscored_news(limit=1000)
        if items:
            await score_items(self.db, items)
            log.info("importance: scored %d items", len(items))


class PriceMemoryJob:
    def __init__(self, db: Database, app: App, exchange: str):
        self.db, self.app, self.exchange = db, app, exchange

    async def __call__(self) -> None:
        saved = await fill_price_marks(self.db, self.app.hub, self.exchange)
        if saved:
            log.info("price memory: recorded %d price points", saved)
            await self.app.similar.refresh()  # rebuilt at most every 10 minutes


class HistoryImportJob:
    """Imports the news archive one GDELT request at a time (GDELT allows one request per 5 s)."""

    def __init__(self, db: Database, enabled: bool = True):
        self.db = db
        self.enabled = enabled
        self._idle_until = 0.0
        self._http = httpx.AsyncClient(headers={"user-agent": "Mozilla/5.0 (compatible; CryptoIntelBot/1.0)"})
        self._backoff = 0.0
        self._last_wait_log = 0.0
        self.waits = 0  # times the archive asked us to slow down
        self._streak = 0  # consecutive refusals, for exponential back-off

    async def close(self) -> None:
        await self._http.aclose()

    async def __call__(self) -> None:
        if not self.enabled or time.monotonic() < max(self._idle_until, self._backoff):
            return
        done = await self.db.kv_keys("history:")
        todo = [step for step in history.plan(utcnow()) if step[0] not in done]
        if not todo:
            self._idle_until = time.monotonic() + 3600  # check again in an hour (a new day adds a window)
            return
        key, topic, start, end = todo[0]
        articles = await history.fetch(self._http, topic, start, end)
        if articles is None:  # rate-limited or temporary error: back off exponentially (30 s … 15 min)
            self.waits += 1
            self._streak += 1
            delay = min(900, 30 * 2 ** (self._streak - 1))
            self._backoff = time.monotonic() + delay
            if time.monotonic() - self._last_wait_log > 300:
                self._last_wait_log = time.monotonic()
                log.info("history import: archive busy (rate limit), next try in %d s — %d requests left", delay, len(todo))
            return
        self._streak = 0
        new = await self.db.add_news(articles, backfill=True, origin="history")
        await self.db.kv_set(key, str(len(new)))
        log.info("history import: %s %s–%s → %d new (%d left)", topic.key, f"{start:%d %b}", f"{end:%d %b}", len(new), len(todo) - 1)


class CalendarJob:
    """Loads this and next week's economic calendar; records past big releases as memory events."""

    def __init__(self, db: Database):
        self.db = db

    async def __call__(self) -> None:
        this_week, next_week = await asyncio.gather(econ_calendar.fetch("this"), econ_calendar.fetch("next"))
        new = await self.db.upsert_calendar(this_week + next_week)
        if new:
            log.info("calendar: %d new events", new)
        released = await self.db.unrecorded_releases()
        if released:
            articles = [
                Article(
                    source="Economic calendar",
                    category="economy",
                    title=(
                        f"📅 {FLAGS.get(e.country, e.country)} {e.country} {e.title} · {e.starts_at:%d %b %Y}"
                        + (f" (forecast {e.forecast}, previous {e.previous})" if e.forecast or e.previous else "")
                    ),
                    url=f"https://www.forexfactory.com/calendar?day={e.starts_at:%b%d.%Y}".lower() + f"#ev{e.id}",
                    summary=None,
                    published_at=e.starts_at,
                    via="Economic calendar",
                )
                for e in released
            ]
            await self.db.add_news(articles, backfill=True, origin="calendar")
            await self.db.mark_recorded([e.id for e in released])


class ReminderJob:
    """Reminds users 30 minutes before high-impact US releases (CPI, NFP, FOMC…)."""

    def __init__(self, db: Database, bot: Bot, lead_minutes: int = 30):
        self.db, self.bot, self.lead = db, bot, lead_minutes

    async def __call__(self) -> None:
        now = utcnow()
        events = [
            e for e in await self.db.calendar_between(now, now + timedelta(minutes=self.lead), min_impact=3)
            if e.country in ("USD", "ALL")
        ]
        if not events:
            return
        for user in await self.db.calendar_subscribers():
            if user.is_muted(now) or user.is_quiet(now):
                continue
            for e in events:
                if await self.db.was_reminded(user.id, e.id):
                    continue
                try:
                    await self.bot.send_message(user.telegram_id, insights.reminder_message(e, user))
                except TelegramForbiddenError:
                    await self.db.update_user(user.telegram_id, calendar_alerts=False, alerts_on=False, digest_on=False)
                    break
                except TelegramAPIError as exc:
                    log.warning("reminder to %s failed: %s", user.telegram_id, exc)
                    continue
                await self.db.record_reminder(user.id, e.id)


class SnapshotJob:
    """Saves the market mood every hour, so the bot can show how sentiment changed over days."""

    def __init__(self, app: App):
        self.app = app

    async def __call__(self) -> None:
        v = await market_view(self.app)
        btc = v.tickers.get("BTC")
        await self.app.db.add_snapshot(
            mood=v.mood.score if v.mood else None,
            fear_greed=float(v.fear_greed.value) if v.fear_greed else None,
            news_index=v.news.index if v.news else None,
            btc_price=btc.last if btc else None,
            funding=v.funding_avg_pct,
            long_pct=v.positioning.long_pct if v.positioning else None,
            open_interest=v.positioning.open_interest_btc if v.positioning else None,
            btc_dominance=v.global_market.btc_dominance if v.global_market else None,
            stablecoins=v.liquidity.supply if v.liquidity else None,
        )


class DigestJob:
    """Sends each subscriber their morning brief at their chosen local hour (once per day)."""

    def __init__(self, app: App, bot: Bot):
        self.app, self.bot = app, bot

    async def send(self, user) -> bool:  # type: ignore[no-untyped-def]
        for text, kb in await insights.digest_messages(self.app, user):
            try:
                await self.bot.send_message(user.telegram_id, text, reply_markup=kb)
            except TelegramForbiddenError:
                await self.app.db.update_user(user.telegram_id, digest_on=False, alerts_on=False, calendar_alerts=False)
                return False
            except TelegramAPIError as exc:
                log.warning("digest to %s failed: %s", user.telegram_id, exc)
                return False
        return True

    async def __call__(self) -> None:
        now = utcnow()
        for user in await self.app.db.digest_subscribers():
            local = user.local(now)
            today = local.date().isoformat()
            if local.hour != user.digest_hour or user.last_digest == today:
                continue
            await self.app.db.update_user(user.telegram_id, last_digest=today)  # mark first: never send twice
            if await self.send(user):
                log.info("digest sent to %s", user.telegram_id)


class RetentionJob:
    def __init__(self, db: Database, days: int):
        self.db, self.days = db, days

    async def __call__(self) -> None:
        removed = await self.db.delete_older_than(self.days)
        if removed:
            log.info("retention: removed %d news items older than %d days", removed, self.days)


class ForecastJob:
    """Every hour: retrain, forecast BTC 4 h / 24 h, save it, and score earlier forecasts whose time has come."""

    def __init__(self, app: App):
        self.app = app

    async def __call__(self) -> None:
        engine = self.app.forecast
        if engine is None:
            return
        fc = await engine.run(force=True)
        if fc is not None:
            for hz, o in fc.outlooks.items():
                await self.app.db.add_forecast(fc.made_at, hz, o.prob_up, fc.price, o.model.reliable)
        due = await self.app.db.due_forecasts()
        if due:
            prices = await prices_at(
                self.app.hub, self.app.settings.price_history_exchange, "BTC",
                [r.made_at + timedelta(hours=r.horizon) for r in due],
            )
            for r in due:
                if (p := prices.get(minute_ms(r.made_at + timedelta(hours=r.horizon)))) is not None:
                    await self.app.db.resolve_forecast(r.id, p)
