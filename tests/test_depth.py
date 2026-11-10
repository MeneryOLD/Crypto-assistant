"""More sources (Telegram, Google News), article view, patterns, Fear & Greed (no network)."""

from datetime import datetime, timedelta

import pytest

from analysis.patterns import patterns, related, summarize, topics_of
from bot import screens
from collectors.fear_greed import parse as parse_fg
from collectors.news import SOURCES, Article, NewsCollector, Source, parse_feed, parse_telegram
from storage.db import Database, NewsItem, Reaction, utcnow

TG_PAGE = """
<div class="tgme_widget_message text_not_supported_wrap js-widget_message" data-post="WalterBloomberg/100" data-view="x">
<div class="tgme_widget_message_text js-message_text" dir="auto">- FED&#39;S POWELL: RATE CUT IN DECEMBER NOT A FOREGONE CONCLUSION<br/>MORE DETAILS TO FOLLOW<br/>(<a href="https://t.me/WalterBloomberg">@WalterBloomberg</a>)</div>
<a class="tgme_widget_message_date" href="https://t.me/WalterBloomberg/100"><time datetime="2026-11-10T19:30:12+00:00" class="time">19:30</time></a>
</div>
<div class="tgme_widget_message js-widget_message" data-post="WalterBloomberg/101" data-view="y">
<div class="tgme_widget_message_photo_wrap"></div>
<a class="tgme_widget_message_date"><time datetime="2026-11-10T19:31:00+00:00"></time></a>
</div>
"""

GOOGLE_RSS = """<?xml version="1.0"?><rss version="2.0"><channel><title>g</title>
<item><title>Fed holds rates steady as inflation cools - Reuters</title><link>https://news.google.com/rss/articles/abc</link>
<description>&lt;a href="x"&gt;Fed holds rates steady&lt;/a&gt;</description><source url="https://reuters.com">Reuters</source>
<pubDate>Tue, 10 Nov 2026 18:00:00 GMT</pubDate></item></channel></rss>"""


def item(id: int, title: str, source: str = "BBC", minutes_ago: int = 0, category: str = "politics") -> NewsItem:
    ts = utcnow() - timedelta(minutes=minutes_ago)
    return NewsItem(id=id, title=title, source=source, category=category, url=f"u{id}", title_hash=str(id), event_at=ts)


def test_sources_are_unique_and_varied():
    names = [s.name for s in SOURCES]
    assert len(names) == len(set(names))
    assert len(SOURCES) >= 80
    assert {s.kind for s in SOURCES} == {"rss", "google", "telegram"}


def test_parse_telegram():
    [a] = parse_telegram(Source("Walter Bloomberg", "u", "economy", "telegram"), TG_PAGE)
    assert a.title == "FED'S POWELL: RATE CUT IN DECEMBER NOT A FOREGONE CONCLUSION"
    assert a.summary == "MORE DETAILS TO FOLLOW"
    assert a.url == "https://t.me/WalterBloomberg/100"
    assert a.published_at == datetime(2026, 11, 10, 19, 30, 12)
    assert a.via == "Walter Bloomberg"


def test_parse_google_news_uses_publisher():
    [a] = parse_feed(Source("Google News: Fed", "u", "economy", "google"), GOOGLE_RSS)
    assert a.title == "Fed holds rates steady as inflation cools"
    assert a.source == "Reuters" and a.via == "Google News: Fed"
    assert a.summary is None


def test_collector_only_polls_due_sources():
    c = NewsCollector()
    assert len(c.due()) == len(SOURCES)
    c.status[SOURCES[0].name].next_due = 1e18
    assert len(c.due()) == len(SOURCES) - 1


def test_fear_greed_parse():
    fg = parse_fg({"data": [{"value": "32", "value_classification": "Fear"}] + [{"value": str(v)} for v in range(29, 36)]})
    assert fg and fg.value == 32 and fg.label == "Fear" and fg.yesterday == 29 and fg.week_ago == 35
    assert fg.emoji == "😨"
    assert "Fear &amp; Greed: <b>32</b> Fear (+3 vs yesterday)" in screens.fear_greed_line(fg)


def test_topics():
    assert "🏦 Fed & rates" in topics_of(item(1, "Powell signals rate cut"))
    assert "🇺🇸 Trump" in topics_of(item(2, "Trump announces new tariffs on China"))
    assert "🚢 Tariffs & sanctions" in topics_of(item(2, "Trump announces new tariffs on China"))
    assert topics_of(item(3, "Local bakery wins award")) == []


def test_patterns_count_busy_moments_once():
    base = utcnow().replace(minute=0, second=0, microsecond=0)
    rs = [
        # three Fed stories in the same half hour: one event
        Reaction(item(1, "Fed cuts rates"), 100, {60: 1.0, 240: 2.0}),
        Reaction(item(2, "Powell speaks on rates"), 100, {60: 1.0, 240: 2.0}),
        Reaction(item(3, "FOMC decision"), 100, {60: 1.0, 240: 2.0}),
        Reaction(item(4, "Fed minutes released"), 100, {60: -1.0, 240: -3.0}),
    ]
    for i, r in enumerate(rs):
        r.item.event_at = base + timedelta(minutes=5 * i if i < 3 else 120)
    p = summarize("Fed", rs)
    assert p and p.events == 2
    assert p.avg[60] == pytest.approx(0.0) and p.avg[240] == pytest.approx(-0.5)
    assert p.up_share[240] == pytest.approx(0.5)
    baseline, groups = patterns(rs)
    assert baseline and groups[0].name == "🏦 Fed & rates"


def test_related_finds_same_story_elsewhere():
    main = item(1, "Israel and Hamas agree to Gaza ceasefire deal", "BBC")
    others = [
        item(2, "Gaza ceasefire deal agreed between Israel and Hamas", "Reuters"),
        item(3, "Israel, Hamas agree ceasefire deal for Gaza: officials", "Al Jazeera"),
        item(4, "Bitcoin climbs above $75,000", "CoinDesk"),
        item(5, "Gaza ceasefire deal agreed between Israel and Hamas", "BBC"),  # same outlet
    ]
    assert sorted(n.source for n in related(main, others)) == ["Al Jazeera", "Reuters"]


@pytest.fixture
async def db():
    d = Database(":memory:")
    await d.init()
    yield d
    await d.close()


async def test_article_screen(db):
    now = utcnow()
    new = await db.add_news(
        [
            Article("BBC", "politics", "Israel and Hamas agree to Gaza ceasefire deal", "https://bbc/1", "Talks in Cairo ended…", now),
            Article("Reuters", "politics", "Gaza ceasefire deal agreed between Israel and Hamas", "https://r/1", None, now - timedelta(minutes=3)),
        ]
    )
    await db.add_marks([(new[0].id, "BTC", 0, 100.0), (new[0].id, "BTC", 15, 101.0), (new[0].id, "ETH", 0, 10.0)])
    text, kb = await screens.article(db, new[0].id, cat="politics", page=1)
    assert "Gaza ceasefire" in text and "Talks in Cairo" in text and "expandable" in text
    assert "+1.00%" in text  # BTC +15m
    assert "Also reported by 1 other outlet" in text and "First reported by Reuters" in text
    assert kb.inline_keyboard[0][0].url == "https://bbc/1"
    assert kb.inline_keyboard[1][0].callback_data == screens.NewsCB(cat="politics", page=1).pack()


async def test_news_list_has_number_buttons(db):
    await db.add_news([Article("BBC", "politics", f"Story {i}", f"u{i}", None, utcnow()) for i in range(3)])
    _, kb = await screens.news(db)
    assert [b.text for b in kb.inline_keyboard[0]] == ["1", "2", "3"]
    assert kb.inline_keyboard[0][0].callback_data.startswith("it:")


async def test_patterns_screen(db):
    text, kb = await screens.history(db, mode="tp")
    assert "Patterns" in text and "Not enough history yet" in text
    assert "• 🏷 By topic •" in [b.text for b in kb.inline_keyboard[0]]


def test_google_news_skips_small_outlets():
    small = GOOGLE_RSS.replace(">Reuters<", ">Hamilton College<").replace(" - Reuters", " - Hamilton College")
    assert parse_feed(Source("Google News: Fed", "u", "economy", "google"), small) == []


def test_telegram_signature_removed_and_kinds_filter():
    page = TG_PAGE.replace("MORE DETAILS TO FOLLOW", "").replace("CONCLUSION", "CONCLUSION|FJ")
    [a] = parse_telegram(Source("FinancialJuice", "u", "economy", "telegram"), page)
    assert a.title.endswith("CONCLUSION")
    c = NewsCollector()
    assert {s.kind for s in c.due({"telegram"})} == {"telegram"}
