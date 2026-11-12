"""Technical indicators (math against known values), interpretation, and the screen (no network)."""

import math

import pytest

from analysis import technicals as ta
from bot.forecast_ui import TechCB, technicals_screen
from storage.db import Database
from tests.test_insights import _app


def test_sma_and_ema():
    assert ta.sma([1, 2, 3, 4, 5], 3) == [None, None, 2.0, 3.0, 4.0]
    e = ta.ema([1.0] * 5 + [2.0], 3)
    assert e[2] == 1.0 and e[5] == pytest.approx(1.5)


def test_rsi_extremes_and_balance():
    up = [float(i) for i in range(30)]
    assert ta.rsi(up)[-1] == 100.0  # only gains
    down = [float(30 - i) for i in range(30)]
    assert ta.rsi(down)[-1] == pytest.approx(0.0)
    zigzag = [100 + (1 if i % 2 else -1) for i in range(60)]
    assert 40 < ta.rsi(zigzag)[-1] < 60


def test_macd_sign_follows_trend():
    rising = [100 * 1.01**i for i in range(80)]
    falling = [100 * 0.99**i for i in range(80)]
    assert ta.macd(rising)[0][-1] > 0 and ta.macd(falling)[0][-1] < 0


def test_bollinger_and_atr():
    mid, sd = ta.bollinger([10.0] * 19 + [30.0])[-1]
    assert mid == 11.0 and sd == pytest.approx(math.sqrt(19.0))
    a = ta.atr([11.0] * 20, [9.0] * 20, [10.0] * 20)
    assert a[-1] == pytest.approx(2.0)


def _candles(closes: list[float], step_ms: int, volume: float = 100.0) -> list[list[float]]:
    return [[i * step_ms, c, c * 1.01, c * 0.99, c, volume] for i, c in enumerate(closes)]


def test_analyse_reads_a_downtrend_correctly():
    daily = _candles([200 - i * 0.2 for i in range(400)], ta.DAY_MS)  # steady decline
    weekly = _candles([100 + i * 0.1 for i in range(260)], 7 * ta.DAY_MS)
    h = _candles([120.0 - i * 0.01 for i in range(100)], 3_600_000)
    t = ta.analyse("BTC", daily, weekly, h, h)
    names = {s.name: s for s in t.signals}
    assert names["50-day MA"].vote == -1 and names["200-day MA"].vote == -1
    assert names["50/200 cross"].value == "death cross"
    assert names["RSI daily"].vote == 1  # oversold after a long decline
    assert "200-week MA" in names and t.verdict in ("🔴 Mostly bearish", "⚪ Mixed")
    assert {s.group for s in t.signals} == {"Trend", "Momentum", "Volatility", "Levels", "Volume"}


@pytest.fixture
async def db():
    d = Database(":memory:")
    await d.init()
    yield d
    await d.close()


class CandleHub:
    names = ("binance",)

    async def candles(self, name, coin, since_ms, limit=1000, timeframe="1m"):
        step = {"1d": ta.DAY_MS, "1w": 7 * ta.DAY_MS, "4h": 4 * 3_600_000, "1h": 3_600_000}[timeframe]
        n = {"1d": 400, "1w": 260, "4h": 300, "1h": 100}[timeframe]
        return _candles([100 + 10 * math.sin(i / 20) for i in range(n)], step)


async def test_technicals_screen(db):
    app = _app(db)
    app.hub = CandleHub()  # type: ignore[assignment]
    text, kb = await technicals_screen(app, "eth")
    assert "ETH technical analysis" in text and "200-week MA" in text and "RSI 4h" in text and "MACD" in text
    assert "<b>Trend</b>" in text and "<b>Levels</b>" in text
    data = [b.callback_data for row in kb.inline_keyboard for b in row]
    assert TechCB(coin="BTC").pack() in data and all(len(d.encode()) <= 64 for d in data)
