"""News collection, de-duplication and price memory (no network)."""

from datetime import datetime, timedelta

import pytest

from analysis.reactions import fill_price_marks, minute_ms, prices_at
from collectors.news import Article, Source, classify, parse_feed
from storage.db import ASSETS, HORIZONS, Database, event_time, utcnow


@pytest.fixture
async def db():
    d = Database(":memory:")
    await d.init()
    yield d
    await d.close()


def art(title: str, url: str, category: str = "politics", published: datetime | None = None) -> Article:
    return Article("BBC World", category, title, url, None, published)


class FakeCandleHub:
    """Every minute's price = 100 + minutes since `origin`."""

    def __init__(self, origin: datetime):
        self.origin_ms = minute_ms(origin)
        self.calls = 0

    async def candles(self, name, coin, since_ms, limit=1000):
        self.calls += 1
        now_ms = minute_ms(utcnow())
        out = []
        for i in range(limit):
            ts = since_ms + i * 60_000
            if ts > now_ms:
                break
            price = 100 + (ts - self.origin_ms) / 60_000
            out.append([ts, price, price, price, price, 1.0])
        return out


def test_classify():
    assert classify("politics", "Voters head to the polls in snap election", None) == "elections"
    assert classify("politics", "House prepares for key impeachment vote", "a vote on Friday") == "politics"
    assert classify("politics", "Senate passes bill on stablecoins", None) == "crypto"
    assert classify("economy", "Fed holds rates steady", None) == "economy"
    assert classify("crypto", "SEC sues exchange over unregistered securities", None) == "regulation"
    assert classify("crypto", "Bitcoin rallies past resistance", None) == "crypto"
    assert classify("regulation", "SEC approves bitcoin ETF options", None) == "regulation"


def test_parse_feed():
    rss = """<?xml version="1.0"?><rss version="2.0"><channel><title>t</title>
    <item><title>Election results &amp; reactions</title><link>https://ex.com/a</link>
    <description>&lt;p&gt;Counting <b>continues</b>&lt;/p&gt;</description>
    <pubDate>Mon, 09 Nov 2026 10:00:00 GMT</pubDate></item>
    <item><title></title><link>https://ex.com/b</link></item>
    </channel></rss>"""
    [a] = parse_feed(Source("Ex", "u", "politics"), rss)
    assert a.title == "Election results & reactions"
    assert a.summary == "Counting continues"
    assert a.category == "elections"
    assert a.published_at == datetime(2026, 11, 9, 10)


def test_event_time_ignores_future_dates():
    seen = datetime(2026, 11, 10, 12)
    assert event_time(datetime(2026, 11, 10, 11), seen) == datetime(2026, 11, 10, 11)
    assert event_time(datetime(2026, 11, 10, 13), seen) == seen
    assert event_time(None, seen) == seen


async def test_dedupe_by_url_and_headline(db):
    first = await db.add_news([art("Ceasefire agreed", "https://a/1"), art("Ceasefire agreed!", "https://b/9")])
    assert len(first) == 1  # same headline on two sites
    again = await db.add_news([art("Ceasefire agreed", "https://a/1"), art("New story", "https://a/2")])
    assert [i.title for i in again] == ["New story"]


async def test_recent_news_filters(db):
    await db.add_news([art("Fed cuts rates", "u1", "economy"), art("PM resigns", "u2", "politics")])
    assert [n.title for n in await db.recent_news(category="economy")] == ["Fed cuts rates"]
    assert [n.title for n in await db.recent_news(query="resign")] == ["PM resigns"]
    assert await db.news_stats() == {"economy": 1, "politics": 1}


async def test_price_memory_fills_all_due_points(db):
    published = utcnow().replace(second=0, microsecond=0) - timedelta(days=2)
    [item] = await db.add_news([art("Old story", "u1", published=published)])
    hub = FakeCandleHub(origin=published)

    saved = await fill_price_marks(db, hub)  # type: ignore[arg-type]
    assert saved == len(HORIZONS) * len(ASSETS)  # 2 days old: every horizon is due
    assert await db.missing_marks() == []
    assert await fill_price_marks(db, hub) == 0  # type: ignore[arg-type]

    [r] = await db.reactions(horizon=60)
    assert r.item.id == item.id and r.price_0 == 100
    assert r.changes[60] == pytest.approx(60.0)  # 100 -> 160
    assert r.changes[1440] == pytest.approx(1440.0)


async def test_fresh_news_only_gets_due_points(db):
    published = utcnow() - timedelta(minutes=30)
    await db.add_news([art("Breaking", "u1", published=published)])
    due = {m for _, _, m, _ in await db.missing_marks()}
    assert due == {0, 15}  # +1h, +4h, +24h are still in the future


async def test_prices_at_uses_few_calls():
    origin = utcnow() - timedelta(days=1)
    hub = FakeCandleHub(origin)
    times = [origin + timedelta(minutes=m) for m in (0, 15, 60, 240)]
    prices = await prices_at(hub, "binance", "BTC", times)  # type: ignore[arg-type]
    assert hub.calls == 1  # all within one 1000-minute window
    assert prices[minute_ms(times[2])] == pytest.approx(160)

