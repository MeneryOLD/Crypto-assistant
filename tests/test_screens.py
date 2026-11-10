"""Screens render correctly and every button carries valid data (no network)."""

from datetime import timedelta

import pytest

from bot import screens
from collectors.exchanges import ExchangeHub
from collectors.news import Article, NewsCollector
from storage.db import Database, utcnow
from tests.test_exchanges import FakeExchange


@pytest.fixture
async def db():
    d = Database(":memory:")
    await d.init()
    yield d
    await d.close()


def hub_with(prices: dict[str, float], failing: tuple[str, ...] = ()) -> ExchangeHub:
    fakes = {n: FakeExchange(n, p, n in failing) for n, p in prices.items()}
    return ExchangeHub(list(prices), factory=lambda n: fakes[n])


def buttons(kb) -> list:
    return [b for row in kb.inline_keyboard for b in row]


def assert_valid_buttons(kb) -> None:
    for b in buttons(kb):
        assert len(b.callback_data.encode()) <= 64, b.callback_data


async def add_items(db: Database, n: int, category: str = "politics") -> None:
    now = utcnow()
    await db.add_news(
        [Article("BBC", category, f"Story number {i}", f"https://x/{category}/{i}", None, now - timedelta(minutes=i)) for i in range(n)]
    )


async def test_news_pages_and_tabs(db):
    await add_items(db, 14)
    text, kb = await screens.news(db)
    assert "page 1/3" in text and "Story number 0" in text
    labels = [b.text for b in buttons(kb)]
    assert "• All •" in labels and "▶️" in labels and "◀️" not in labels
    assert_valid_buttons(kb)

    text, kb = await screens.news(db, page=2)
    assert "page 3/3" in text and "Story number 13" in text
    assert "▶️" not in [b.text for b in buttons(kb)]


async def test_news_category_and_search(db):
    await add_items(db, 2, "economy")
    await add_items(db, 2, "politics")
    text, _ = await screens.news(db, cat="economy")
    assert "📊" in text and "🏛" not in text
    text, kb = await screens.news(db, q="number 1")
    assert "News about" in text and "Story number 1" in text
    assert "📰 All news" in [b.text for b in buttons(kb)]


async def test_news_empty(db):
    text, _ = await screens.news(db, cat="elections")
    assert "Nothing here yet" in text


async def test_market_screen():
    hub = hub_with({"binance": 100.0, "okx": 101.0, "bybit": 0.0}, failing=("bybit",))
    text, kb = await screens.market(hub, "btc")
    assert "BTC" in text and "<pre>" in text and "Binance" in text and "OKX" in text
    assert "Not available: Bybit" in text
    assert "• BTC •" in [b.text for b in buttons(kb)]
    assert_valid_buttons(kb)


async def test_market_unknown_coin_keeps_tabs():
    hub = hub_with({"binance": 100.0})
    text, kb = await screens.market(hub, "nope")
    assert "isn't available" in text and buttons(kb)


async def test_order_book_and_gap():
    hub = hub_with({"binance": 100.0, "okx": 103.0})
    text, kb = await screens.order_book(hub, "BTC", "okx")
    assert "on <b>OKX</b>" in text and "sell" in text and "buy" in text
    assert "• OKX •" in [b.text for b in buttons(kb)]
    text, _ = await screens.buy_sell_gap(hub, "BTC")
    assert "Buy on <b>Binance</b>" in text and "Sell on <b>OKX</b>" in text


async def test_history_and_status(db):
    hub = hub_with({"binance": 100.0})
    text, kb = await screens.history(db, hz=240)
    assert "Nothing yet" in text and "• 4h •" in [b.text for b in buttons(kb)]
    assert_valid_buttons(kb)

    collector = NewsCollector()
    collector.status["CoinDesk"].ok = False
    collector.status["CoinDesk"].error = "timed out"
    text, kb = await screens.status(db, hub, collector)
    assert "Binance" in text and "CoinDesk: timed out" in text
    text, _ = await screens.status(db, hub, collector, full=True)
    assert "Bitcoin Magazine" in text


async def test_home(db):
    await add_items(db, 3)
    text = await screens.home(db, hub_with({"binance": 100.0}), NewsCollector(), FakeFG(), "Sam")
    assert "Hi, Sam" in text and "BTC" in text and "Story number 0" in text


class FakeFG:
    async def get(self):
        return None
