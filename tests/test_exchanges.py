"""Exchange hub tests with fake exchanges (no network)."""

import pytest

from collectors.exchanges import ExchangeError, ExchangeHub, Ticker, best_spread, pick_symbol

MARKETS = {
    "BTC/USDT": {"spot": True, "active": True},
    "ETH/USD": {"spot": True, "active": True},
    "OLD/USDT": {"spot": True, "active": False},
}


class FakeExchange:
    def __init__(self, name: str, price: float, fail: bool = False):
        self.name, self.price, self.fail = name, price, fail
        self.markets: dict = {}
        self.loads = 0

    async def load_markets(self):
        self.loads += 1
        if self.fail:
            raise RuntimeError("geo-blocked")
        self.markets = MARKETS

    async def fetch_ticker(self, symbol):
        p = self.price
        return {"last": p, "bid": p - 1, "ask": p + 1, "percentage": 1.5, "high": p + 50, "low": p - 50, "quoteVolume": 2e9}

    async def fetch_order_book(self, symbol, limit=None):
        return {"bids": [[self.price - i, 0.5] for i in range(1, 30)], "asks": [[self.price + i, 0.5] for i in range(1, 30)]}

    async def close(self):
        pass


def make_hub(prices: dict[str, float], failing: tuple[str, ...] = ()) -> tuple[ExchangeHub, dict]:
    fakes = {n: FakeExchange(n, p, n in failing) for n, p in prices.items()}
    return ExchangeHub(list(prices), factory=lambda n: fakes[n]), fakes


def ticker(ex: str, bid: float, ask: float) -> Ticker:
    return Ticker(ex, "BTC/USDT", (bid + ask) / 2, bid, ask, None, None, None, None)


def test_pick_symbol_prefers_usdt_and_skips_inactive():
    assert pick_symbol(MARKETS, "btc") == "BTC/USDT"
    assert pick_symbol(MARKETS, "eth") == "ETH/USD"
    assert pick_symbol(MARKETS, "old") is None
    assert pick_symbol(MARKETS, "doge") is None


def test_best_spread():
    s = best_spread([ticker("binance", 99, 100), ticker("okx", 101, 102), ticker("kraken", 98, 99)])
    assert s is not None
    assert s.buy.exchange == "kraken" and s.sell.exchange == "okx"
    assert s.pct == pytest.approx((101 - 99) / 99 * 100)
    assert best_spread([ticker("binance", 99, 100)]) is None


def test_unknown_exchange_rejected():
    with pytest.raises(ValueError):
        ExchangeHub(["notanexchange"])


async def test_ticker_and_markets_loaded_once():
    hub, fakes = make_hub({"binance": 100.0})
    t = await hub.ticker("binance", "btc")
    await hub.ticker("binance", "btc")
    assert t.symbol == "BTC/USDT" and t.last == 100.0 and t.bid == 99.0
    assert fakes["binance"].loads == 1


async def test_coin_not_listed():
    hub, _ = make_hub({"binance": 100.0})
    with pytest.raises(ExchangeError, match="not traded"):
        await hub.ticker("binance", "doge")


async def test_tickers_everywhere_collects_errors():
    hub, _ = make_hub({"binance": 100.0, "okx": 105.0, "bybit": 0.0}, failing=("bybit",))
    tickers, errors = await hub.tickers_everywhere("btc")
    assert sorted(t.exchange for t in tickers) == ["binance", "okx"]
    assert "geo-blocked" in errors["bybit"]


async def test_order_book_depth():
    hub, _ = make_hub({"okx": 100.0})
    ob = await hub.order_book("okx", "btc", depth=5)
    assert len(ob.bids) == 5 and ob.bids[0] == (99.0, 0.5) and ob.asks[0] == (101.0, 0.5)


async def test_status():
    hub, _ = make_hub({"binance": 100.0, "bybit": 0.0}, failing=("bybit",))
    statuses = {s.exchange: s for s in await hub.status()}
    assert statuses["binance"].ok and statuses["binance"].markets == 3
    assert not statuses["bybit"].ok
