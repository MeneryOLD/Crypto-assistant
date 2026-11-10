from datetime import datetime, timezone

import pytest

from app import services
from app.config import normalize_database_url
from app.db import create_engine, create_session_factory, init_db
from app.formatting import fmt_big, fmt_price, split_message
from app.market.coingecko import CoinMarket, CoinRef, match_coin
from app.models import AlertKind, NewsItem
from app.news.collector import Article, mentions, parse_feed, recent_news, store_articles, title_fingerprint
from app.research.assistant import Evidence, format_report
from app.research.evidence import coins_mentioned

BTC = CoinRef("bitcoin", "BTC", "Bitcoin")
ETH = CoinRef("ethereum", "ETH", "Ethereum")


def market(coin: CoinRef, price: float, change: float | None = 0.0) -> CoinMarket:
    return CoinMarket(coin.coin_id, coin.symbol, coin.name, price, change_24h=change)


@pytest.fixture
async def session():
    engine = create_engine("sqlite+aiosqlite:///:memory:")
    await init_db(engine)
    async with create_session_factory(engine)() as s:
        yield s
    await engine.dispose()


async def make_user(session, tid=1):
    return await services.upsert_user(
        session, telegram_id=tid, chat_id=tid, username="u", first_name="Test", last_name=None
    )


# --- pure helpers ----------------------------------------------------------


def test_normalize_database_url():
    assert (
        normalize_database_url("postgresql://postgres:pw@db:5432/telegram_bot?schema=public")
        == "postgresql+asyncpg://postgres:pw@db:5432/telegram_bot"
    )
    assert normalize_database_url("sqlite:///./x.db") == "sqlite+aiosqlite:///./x.db"
    assert normalize_database_url("sqlite+aiosqlite:///./x.db") == "sqlite+aiosqlite:///./x.db"


def test_formatting():
    assert fmt_price(104_250.4) == "$104,250"
    assert fmt_price(2.5) == "$2.50"
    assert fmt_price(0.00001234) == "$0.00001234"
    assert fmt_big(2.5e12) == "$2.50T"


def test_split_message_respects_limit():
    text = "\n\n".join("x" * 900 for _ in range(10))
    chunks = split_message(text, limit=4096)
    assert all(len(c) <= 4096 for c in chunks)
    assert "".join(chunks).replace("\n", "") == text.replace("\n", "")


def test_match_coin_prefers_id_then_symbol_then_name():
    coins = [BTC, ETH]
    assert match_coin("btc", coins) == BTC
    assert match_coin("Ethereum", coins) == ETH
    assert match_coin("doge", coins) is None


def test_title_fingerprint_ignores_case_and_punctuation():
    assert title_fingerprint("Bitcoin hits $100K!") == title_fingerprint("bitcoin hits 100k")


def test_mentions_whole_words_only():
    item = NewsItem(title="Ethereum ETF inflows rise", summary="SOL also up", url="u", source="s", title_hash="h")
    assert mentions(item, ["Ethereum"])
    assert mentions(item, ["SOL"])
    assert not mentions(item, ["ETH"])  # 'ETF'/'Ethereum' must not match 'ETH'


def test_parse_feed():
    rss = """<?xml version="1.0"?><rss version="2.0"><channel><title>t</title>
    <item><title>BTC &amp; ETH rally</title><link>https://ex.com/a</link>
    <description>&lt;p&gt;Markets <b>up</b>&lt;/p&gt;</description>
    <pubDate>Mon, 09 Nov 2026 10:00:00 GMT</pubDate></item>
    <item><title></title><link>https://ex.com/b</link></item>
    </channel></rss>"""
    articles = parse_feed("Ex", rss)
    assert len(articles) == 1
    a = articles[0]
    assert a.title == "BTC & ETH rally"
    assert a.summary == "Markets up"
    assert a.published_at == datetime(2026, 11, 9, 10, tzinfo=timezone.utc)


def test_alert_trigger_rules():
    above = services.PriceAlert(kind=AlertKind.ABOVE, threshold=100)
    below = services.PriceAlert(kind=AlertKind.BELOW, threshold=100)
    change = services.PriceAlert(kind=AlertKind.CHANGE, threshold=5)
    assert services.alert_is_triggered(above, market(BTC, 101))
    assert not services.alert_is_triggered(above, market(BTC, 99))
    assert services.alert_is_triggered(below, market(BTC, 99))
    assert services.alert_is_triggered(change, market(BTC, 1, change=-6.1))
    assert not services.alert_is_triggered(change, market(BTC, 1, change=None))


def test_report_lists_only_valid_cited_sources():
    news = [NewsItem(source="CoinDesk", title="BTC ETF outflows", url="https://cd/1", title_hash="a")]
    ev = Evidence(markets=[market(BTC, 100_000)], news=news)
    report = format_report("Outflows continued [N1]. Price [M1]. Bogus [N9].", ev)
    assert "[N1] CoinDesk: BTC ETF outflows https://cd/1" in report
    assert "[M1] CoinGecko market data: Bitcoin" in report
    assert "[N9] " not in report.split("SOURCES")[1]
    assert "Not financial advice" in report


def test_evidence_render_contains_ids():
    ev = Evidence(markets=[market(BTC, 100_000)], news=[])
    text = ev.render()
    assert "[M1] Bitcoin (BTC)" in text
    assert "no relevant news" in text


def test_coins_mentioned():
    markets = [market(BTC, 1), market(ETH, 1)]
    assert [m.symbol for m in coins_mentioned("Why is bitcoin falling?", markets)] == ["BTC"]
    assert [m.symbol for m in coins_mentioned("ETH outlook", markets)] == ["ETH"]


# --- database-backed services ---------------------------------------------


async def test_upsert_user_is_idempotent(session):
    u1 = await make_user(session)
    u2 = await services.upsert_user(session, telegram_id=1, chat_id=1, username="new", first_name="T", last_name=None)
    assert u1.id == u2.id and u2.username == "new"


async def test_watchlist_add_remove(session):
    user = await make_user(session)
    assert await services.add_to_watchlist(session, user, BTC)
    assert not await services.add_to_watchlist(session, user, BTC)
    assert [i.coin_id for i in await services.get_watchlist(session, user)] == ["bitcoin"]
    assert await services.remove_from_watchlist(session, user, "bitcoin")
    assert await services.get_watchlist(session, user) == []


async def test_alerts_trigger_once(session):
    user = await make_user(session)
    await services.add_alert(session, user, BTC, AlertKind.ABOVE, 100_000)
    await services.add_alert(session, user, ETH, AlertKind.BELOW, 2_000)
    markets = {"bitcoin": market(BTC, 105_000), "ethereum": market(ETH, 3_000)}

    first = await services.evaluate_alerts(session, markets)
    assert [t.alert.symbol for t in first] == ["BTC"]
    assert await services.evaluate_alerts(session, markets) == []
    assert [a.symbol for a in await services.get_alerts(session, user)] == ["ETH"]


async def test_add_alert_rejects_non_positive(session):
    user = await make_user(session)
    with pytest.raises(ValueError):
        await services.add_alert(session, user, BTC, AlertKind.ABOVE, 0)


async def test_news_dedupe_and_watchlist_notifications(session):
    user = await make_user(session)
    await services.add_to_watchlist(session, user, ETH)
    now = datetime.now(timezone.utc)
    articles = [
        Article("A", "Ethereum upgrade ships", "https://a/1", None, now),
        Article("B", "Ethereum Upgrade Ships!", "https://b/1", None, now),  # same story, other outlet
        Article("A", "Bitcoin miners sell", "https://a/2", None, now),
    ]
    new = await store_articles(session, articles)
    assert len(new) == 2
    assert await store_articles(session, articles) == []  # second run stores nothing

    notes = await services.match_news_to_watchlists(session, new)
    assert [(n.chat_id, n.item.title, n.coins) for n in notes] == [(1, "Ethereum upgrade ships", ["ETH"])]
    assert all(i.notified for i in new)

    assert [n.title for n in await recent_news(session, terms=["Bitcoin"])] == ["Bitcoin miners sell"]
