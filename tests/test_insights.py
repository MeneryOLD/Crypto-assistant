"""Stages 4–6: similar past events, sentiment & mood, calendar, digest, history import, new sources."""

from datetime import datetime, timedelta

import pytest

from analysis.sentiment import gauge, headline_tone, market_mood, news_sentiment, sparkline
from analysis.similar import SimilarityEngine, horizon_stats
from bot import insights
from bot.context import App, MarketView
from bot.scheduler import JobState, Scheduler
from collectors import calendar as econ_calendar
from collectors import history
from collectors.market_data import parse_global, parse_positioning, parse_stablecoins
from collectors.news import Source, parse_cryptopanic, parse_feed, parse_telegram, parse_x, premium_sources, usd_amount
from config import Settings
from storage.db import Database, NewsItem, Reaction, User, utcnow

# --- sentiment ---------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("title", "sign"),
    [
        ("Bitcoin surges to record high as ETF inflows jump", 1),
        ("Fed cuts rates by 25 bps", 1),
        ("Exchange hacked, $200M drained", -1),
        ("Fed raises rates, signals more tightening", -1),
        ("Russia launches missile strike on Kyiv", -1),
        ("Israel and Hamas agree ceasefire", 1),
        ("Major blockchains have not been hacked, CEO says", 0),
        ("Weekly market wrap: what to know", 0),
    ],
)
def test_headline_tone(title, sign):
    tone = headline_tone(title)
    assert (tone > 0) - (tone < 0) == sign, tone


def _item(i: int, title: str, importance: int = 5, category: str = "crypto", minutes_ago: int = 0, source: str = "CoinDesk") -> NewsItem:
    return NewsItem(id=i, title=title, importance=importance, category=category, source=source, url=f"u{i}",
                    title_hash=str(i), event_at=utcnow() - timedelta(minutes=minutes_ago), origin="live")


def test_news_sentiment_weights_important_news():
    items = [_item(1, "Bitcoin surges", 9), _item(2, "Altcoin dips", 3), _item(3, "Token drops", 3)]
    s = news_sentiment(items)
    assert s.bullish == 1 and s.bearish == 2
    assert s.index > 60  # one important bullish story outweighs two minor bearish ones
    assert s.top_bullish is items[0]


def test_market_mood_blends_and_skips_missing():
    m = market_mood(fear_greed=20, news_index=None, btc_change_24h=-3, btc_change_7d=-8, funding_avg_pct=None, long_pct=None)
    assert m and m.score < 30 and set(m.components) == {"Fear & Greed", "Price momentum"}
    assert m.label in ("Extreme fear", "Fear")
    assert market_mood(fear_greed=None, news_index=None, btc_change_24h=None, btc_change_7d=None,
                       funding_avg_pct=None, long_pct=None) is None


def test_sparkline_and_gauge():
    assert sparkline([0, 50, 100], 0, 100) == "▁▄█"
    assert gauge(50, 10) == "▰▰▰▰▰▱▱▱▱▱"


# --- similar past events --------------------------------------------------------------------


def _reaction(i: int, title: str, days_ago: float, m4: float, m24: float = 0.0, m1: float = 0.0) -> Reaction:
    item = NewsItem(id=i, title=title, category="economy", source=f"S{i}", url=f"u{i}", title_hash=str(i),
                    event_at=utcnow() - timedelta(days=days_ago))
    return Reaction(item, 100.0, {60: m1, 240: m4, 1440: m24})


def test_similarity_engine_finds_similar_and_builds_signal():
    pool = [_reaction(i, f"Fed raises interest rates by 25 bps as inflation persists {i}", 2 + i, -1.0 - i * 0.1, -2.0) for i in range(8)]
    pool += [_reaction(100 + i, f"Bitcoin miners report record hashrate {i}", 2 + i, 0.5) for i in range(20)]
    engine = SimilarityEngine(db=None)  # type: ignore[arg-type]
    engine.load(pool)
    assert engine.size == 28

    probe = NewsItem(id=999, title="Fed raises interest rates again to fight inflation", category="economy",
                     source="X", url="p", title_hash="p", event_at=utcnow())
    import asyncio
    outlook = asyncio.run(_for_item(engine, probe))
    assert outlook is not None and outlook.events == 8
    assert all("Fed" in e.reaction.item.title for e in outlook.examples)
    assert outlook.signal == "🔴 Bearish lean"
    assert outlook.stats[240].up_share == 0 and outlook.volatility_ratio and outlook.volatility_ratio > 1
    text = insights.outlook_text(probe.title, outlook, engine.size)
    assert "Bearish lean" in text and "<pre>" in text and "Most similar past events" in text


async def _for_item(engine: SimilarityEngine, item: NewsItem):
    engine._built = 1e18  # skip the database refresh in this unit test
    return await engine.for_item(item)


def test_similar_ignores_the_same_moment():
    engine = SimilarityEngine(db=None)  # type: ignore[arg-type]
    engine.load([_reaction(1, "SEC sues major crypto exchange over securities", 0.1, -2.0)])
    engine._built = 1e18
    probe = NewsItem(id=2, title="SEC sues major crypto exchange", category="regulation", source="Y", url="q",
                     title_hash="q", event_at=utcnow())
    import asyncio
    assert asyncio.run(engine.for_item(probe)) is None  # 2.4 h apart = same event, not history


def test_horizon_stats():
    s = horizon_stats([-1.0, 1.0, 3.0])
    assert s and s.median == 1.0 and s.up_share == pytest.approx(2 / 3) and s.worst == -1 and s.mean_abs == pytest.approx(5 / 3)


# --- data sources ----------------------------------------------------------------------------


def test_market_data_parsers():
    g = parse_global({"data": {"total_market_cap": {"usd": 2.5e12}, "total_volume": {"usd": 8e10},
                               "market_cap_change_percentage_24h_usd": -1.2, "market_cap_percentage": {"btc": 59.7, "eth": 10.1}}})
    assert g.btc_dominance == 59.7 and g.total_mcap == 2.5e12
    liq = parse_stablecoins({"peggedAssets": [
        {"pegType": "peggedUSD", "circulating": {"peggedUSD": 110}, "circulatingPrevDay": {"peggedUSD": 100},
         "circulatingPrevWeek": {"peggedUSD": 100}, "circulatingPrevMonth": {"peggedUSD": 100}},
        {"pegType": "peggedEUR", "circulating": {"peggedEUR": 999}},
    ]})
    assert liq.supply == 110 and liq.change_7d == pytest.approx(10)
    pos = parse_positioning(
        [{"longShortRatio": "3.0", "longAccount": "0.75"}],
        [{"longShortRatio": "2.0"}],
        {"result": {"list": [{"buyRatio": "0.7"}]}},
        [{"sumOpenInterest": "100"}, {"sumOpenInterest": "110"}],
    )
    assert pos.long_pct == 0.75 and pos.bybit_buy_ratio == 0.7 and pos.oi_change_24h == pytest.approx(10)


def test_calendar_parse():
    rows = [
        {"title": "CPI y/y", "country": "USD", "date": "2026-11-12T08:30:00-05:00", "impact": "High", "forecast": "3.0%", "previous": "2.9%"},
        {"title": "", "country": "EUR", "date": "2026-11-12T08:30:00-05:00", "impact": "Low"},
        {"title": "Bank Holiday", "country": "JPY", "date": "bad-date", "impact": "Holiday"},
    ]
    [e] = econ_calendar.parse(rows)
    assert e.starts_at == datetime(2026, 11, 12, 13, 30) and e.impact == 3 and e.flag == "🇺🇸"


def test_history_plan_is_stable_and_parse_filters():
    a = history.plan(datetime(2026, 11, 10, 9))
    b = history.plan(datetime(2026, 11, 10, 22))
    assert [k for k, *_ in a] == [k for k, *_ in b]  # same day -> same requests
    c = history.plan(datetime(2026, 11, 11, 9))
    assert {k for k, *_ in a if "-" not in k.split(":")[-1]} <= {k for k, *_ in c}  # finished windows never move
    assert len(a) == len(history.TOPICS) * len(history.windows(datetime(2026, 11, 10)))

    topic = next(t for t in history.TOPICS if t.key == "fed")
    data = {"articles": [
        {"url": "https://reuters.com/a", "title": "Fed holds rates steady as inflation cools down", "seendate": "20261020T143000Z", "domain": "reuters.com", "language": "English"},
        {"url": "https://x.com/b", "title": "Local bakery wins national bread award again", "seendate": "20261020T143000Z", "domain": "x.com", "language": "English"},
        {"url": "https://y.com/c", "title": "Fed decision expected to shake markets today", "seendate": "20261020T150000Z", "domain": "y.com", "language": "Spanish"},
    ]}
    [art] = history.parse(topic, data)
    assert art.source == "Reuters" and art.published_at == datetime(2026, 10, 20, 14, 30) and art.via == "GDELT archive"


def test_whale_alert_filter_and_usd_amount():
    assert usd_amount("🚨 1,000 #BTC (73,500,000 USD) transferred") == 73_500_000
    assert usd_amount("Fund raises $1.2 billion") == 1.2e9
    page = """
<div class="tgme_widget_message js-widget_message" data-post="whale_alert_io/1" data-view="x">
<div class="tgme_widget_message_text js-message_text">🚨 1,000 #BTC (73,500,000 USD) transferred from Binance to unknown wallet</div></div>
<div class="tgme_widget_message js-widget_message" data-post="whale_alert_io/2" data-view="y">
<div class="tgme_widget_message_text js-message_text">🚨 100 #ETH (204,000 USD) transferred from unknown wallet to Coinbase</div></div>
"""
    [a] = parse_telegram(Source("Whale Alert", "u", "crypto", "telegram", min_usd=50_000_000), page)
    assert "73,500,000 USD" in a.title


def test_reddit_feed_strips_footer():
    atom = """<?xml version="1.0" encoding="UTF-8"?><feed xmlns="http://www.w3.org/2005/Atom">
    <entry><title>SEC approves new crypto ETF rules</title><link href="https://www.reddit.com/r/CryptoCurrency/comments/1/x/"/>
    <updated>2026-11-10T19:00:00+00:00</updated>
    <content type="html">&lt;p&gt;Big news today&lt;/p&gt; submitted by /u/someone [link] [comments]</content></entry></feed>"""
    [a] = parse_feed(Source("r/CryptoCurrency", "u", "crypto", "reddit"), atom)
    assert a.source == "r/CryptoCurrency" and a.summary == "Big news today" and a.category == "regulation"


def test_premium_sources_only_with_keys():
    assert premium_sources() == ()
    srcs = premium_sources("cp-token", "x-token", ["DeItaone", "@WatcherGuru"])
    assert [s.kind for s in srcs] == ["cryptopanic", "x"]
    assert "from%3ADeItaone" in srcs[1].url and "from%3AWatcherGuru" in srcs[1].url


def test_parse_cryptopanic_and_x():
    cp = parse_cryptopanic(Source("CryptoPanic", "u", "crypto", "cryptopanic"), {"results": [
        {"id": 5, "slug": "btc-up", "title": "Bitcoin ETF sees record inflows", "published_at": "2026-11-10T19:00:00Z",
         "source": {"title": "CoinDesk"}, "votes": {"positive": 12, "negative": 1, "important": 4}},
    ]})
    assert cp[0].source == "CoinDesk" and cp[0].url == "https://cryptopanic.com/news/5/btc-up" and "👍 12" in cp[0].summary
    x = parse_x(Source("X (Twitter)", "u", "economy", "x"), {
        "data": [{"id": "9", "text": "FED CUTS RATES BY 25BPS\nmore to come", "created_at": "2026-11-10T19:00:00.000Z", "author_id": "1"},
                 {"id": "10", "text": "RT @someone: old news repeated", "author_id": "1"}],
        "includes": {"users": [{"id": "1", "username": "DeItaone"}]},
    })
    assert len(x) == 1 and x[0].source == "X: @DeItaone" and x[0].url == "https://x.com/DeItaone/status/9"


# --- scheduler ---------------------------------------------------------------------------------


async def test_scheduler_tracks_health():
    s = Scheduler()
    calls = {"ok": 0}

    async def ok():
        calls["ok"] += 1

    async def bad():
        raise RuntimeError("boom")

    s.add("ok", 0.01, ok)
    s.add("bad", 0.01, bad)
    s.start()
    import asyncio
    await asyncio.sleep(0.12)
    await s.stop()
    assert calls["ok"] >= 3 and s.jobs["ok"].healthy
    assert not s.jobs["bad"].healthy and "boom" in s.jobs["bad"].last_error
    assert [j.name for j in s.unhealthy] == ["bad"]
    assert JobState("x", 60).healthy  # never run yet = fine


# --- database + screens -------------------------------------------------------------------------


@pytest.fixture
async def db():
    d = Database(":memory:")
    await d.init()
    yield d
    await d.close()


class _NoHub:
    names = ("binance",)

    async def ticker(self, *a):
        raise RuntimeError("offline")

    async def change_7d(self, *a):
        return None

    async def funding_rates(self, *a):
        return {"binance": 0.0001}


class _Off:
    async def get(self):
        return None

    async def global_market(self):
        return None

    async def positioning(self):
        return None

    async def liquidity(self):
        return None

    async def network(self):
        return None


def _app(db: Database) -> App:
    return App(settings=Settings(bot_token="x", admin_ids="42"), db=db, hub=_NoHub(), collector=None, fg=_Off(),  # type: ignore[arg-type]
               market=_Off(), similar=SimilarityEngine(db), patterns=None)  # type: ignore[arg-type]


async def test_calendar_screen_reminder_and_recording(db):
    now = utcnow()
    events = [
        econ_calendar.CalendarEvent("CPI y/y", "USD", now + timedelta(minutes=20), 3, "3.0%", "2.9%"),
        econ_calendar.CalendarEvent("ZEW Sentiment", "EUR", now + timedelta(hours=5), 2, "", ""),
        econ_calendar.CalendarEvent("Retail Sales", "USD", now - timedelta(hours=1), 3, "0.2%", "0.1%"),
    ]
    assert await db.upsert_calendar(events) == 3
    assert await db.upsert_calendar(events) == 0  # idempotent
    user, _ = await db.upsert_user(1, "a", "A")
    await db.update_user(1, tz_offset=180)
    user = await db.get_user(1)
    text, kb = await insights.calendar(_app(db), user, week=0, imp=2)
    assert "CPI y/y" in text and "UTC+3" in text and "⏰ in " in text
    assert all(len(b.callback_data.encode()) <= 64 for row in kb.inline_keyboard for b in row)
    [due] = await db.calendar_between(now, now + timedelta(minutes=30), min_impact=3)
    assert "In 30 minutes" in insights.reminder_message(due, user)
    released = await db.unrecorded_releases()
    assert [e.title for e in released] == ["Retail Sales"]


async def test_digest_settings_and_messages(db):
    user, _ = await db.upsert_user(1, "a", "Sam")
    app = _app(db)
    text, kb = await insights.digest_settings(app, user)
    assert "ON ✅" in text and "08:00" in text
    assert all(len(b.callback_data.encode()) <= 64 for row in kb.inline_keyboard for b in row)
    await db.add_news([])  # nothing yet: digest must still render
    msgs = await insights.digest_messages(app, user)
    assert len(msgs) == 2 and "Sam" in msgs[0][0] and "Top 10 stories" in msgs[1][0]


async def test_sentiment_screen_renders_with_partial_data(db):
    text, kb = await insights.sentiment(_app(db))
    assert "Market sentiment" in text and "Funding" in text
    assert kb.inline_keyboard[0][0].text == "🔄 Refresh"


async def test_admin_and_kv_and_retention(db):
    app = _app(db)
    assert app.is_admin(42) and not app.is_admin(7)
    await db.kv_set("history:fed:20261020", "12")
    await db.kv_set("history:fed:20261020", "13")
    assert await db.kv_get("history:fed:20261020") == "13"
    assert await db.kv_keys("history:") == {"history:fed:20261020"}
    text, _ = await insights.admin(app)
    assert "Admin" in text and "History import" in text
    from collectors.news import Article
    [old] = await db.add_news([Article("A", "crypto", "Very old story about bitcoin", "u-old", None, utcnow() - timedelta(days=400))])
    await db.add_marks([(old.id, "BTC", 0, 1.0)])
    assert await db.delete_older_than(365) == 1
    assert (await db.stats())["price_marks"] == 0


def test_user_local_time_and_label():
    u = User(tz_offset=330)
    assert u.tz_label == "UTC+5:30"
    assert u.local(datetime(2026, 1, 1, 0, 0)) == datetime(2026, 1, 1, 5, 30)
    assert User(tz_offset=-300).tz_label == "UTC−5"


def test_market_view_funding_avg():
    v = MarketView(funding={"binance": 0.0001, "okx": 0.0003})
    assert v.funding_avg_pct == pytest.approx(0.02)


@pytest.mark.parametrize(
    ("title", "sign"),
    [
        ("US credit card debt reaches new all-time high of $1.29 trillion", -1),
        ("10-year Treasury yield surges above 5%", -1),
        ("Bank of Canada raises policy rate amid inflation concerns", -1),
        ("ECB cuts key rates by 25 bps", 1),
        ("Bitcoin hits all-time high above $120,000", 1),
    ],
)
def test_macro_context_tone(title, sign):
    tone = headline_tone(title)
    assert (tone > 0) - (tone < 0) == sign, tone


def test_history_queries_follow_gdelt_syntax():
    import re as _re
    for t in history.TOPICS:
        q = t.query
        if q.startswith("("):
            inner = q[1:-1]
            assert " OR " in inner, f"{t.key}: parentheses only around OR groups"
            for term in inner.split(" OR "):
                assert term.startswith('"') or " " not in term, f"{t.key}: multi-word term must be quoted: {term}"
        else:
            assert " " not in q.strip('"') or _re.fullmatch(r'"[^"]+"', q), f"{t.key}: bare multi-word query"


async def test_history_import_backs_off_exponentially(db, monkeypatch):
    from bot.jobs import HistoryImportJob

    calls = []

    async def busy(client, topic, start, end):
        calls.append(topic.key)
        return None

    monkeypatch.setattr(history, "fetch", busy)
    job = HistoryImportJob(db)
    import time as _time
    delays = []
    for _ in range(6):
        job._backoff = 0  # pretend the wait passed
        await job()
        delays.append(round(job._backoff - _time.monotonic()))
    await job.close()
    assert delays == [30, 60, 120, 240, 480, 900]
    assert job.waits == 6 and len(calls) == 6
    assert await db.kv_keys("history:") == set()  # nothing marked done while refused
