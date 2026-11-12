"""Forecast: model fitting, honesty checks, features, track record, screen (no network)."""

import math
import random
from datetime import timedelta

import pytest

from analysis import forecast as fc
from analysis import volatility as vol
from analysis.technicals import Signal
from bot.forecast_ui import forecast_line, forecast_screen
from storage.db import Database, NewsItem, Reaction, utcnow
from tests.test_insights import _app

HOUR = fc.HOUR_MS


def _history(closes: list[float], news: dict | None = None) -> fc.History:
    times = [i * HOUR for i in range(len(closes))]
    return fc.History(times=times, closes=closes, fear_greed={}, funding=[], news=news or {})


def test_logistic_regression_learns_a_clear_signal():
    rnd = random.Random(1)
    xs = [[rnd.gauss(0, 1), rnd.gauss(0, 1)] for _ in range(600)]
    ys = [1 if x[0] + 0.1 * rnd.gauss(0, 1) > 0 else 0 for x in xs]  # depends on feature 0 only
    w = fc.fit_logistic(xs, ys, l2=0.1)
    assert w[1] > 2 and abs(w[2]) < 0.5
    assert fc.predict(w, [2.0, 0.0]) > 0.9 and fc.predict(w, [-2.0, 0.0]) < 0.1


def test_random_walk_is_not_called_reliable():
    rnd = random.Random(7)
    closes = [100.0]
    for _ in range(2900):
        closes.append(closes[-1] * math.exp(rnd.gauss(0, 0.004)))
    model = fc.train(_history(closes), 4)
    assert model is not None and model.samples > 600  # every 4th hour of ~2,900
    assert not model.reliable  # no pattern in a random walk -> no claimed edge
    assert model.margin > 0.05
    o = fc.outlook(model, _history(closes))
    assert o and o.verdict == "⚪ No reliable edge" and o.low < closes[-1] < o.high


def test_strong_pattern_is_detected_as_reliable():
    # price oscillates with a 48-hour cycle: "below the recent high" predicts a rise
    closes = [100 * (1 + 0.05 * math.sin(2 * math.pi * i / 48)) for i in range(2900)]
    model = fc.train(_history(closes), 4)
    assert model and model.test_acc > 0.8 and model.reliable


def test_features_use_only_the_past_and_news_needs_coverage():
    closes = [100 + i * 0.1 for i in range(400)]
    h = _history(closes)
    f = fc.features_at(h, 300)
    assert f and f["ret_4h"] == pytest.approx(closes[300] / closes[296] - 1)
    assert fc.features_at(h, 100) is None  # needs 7 days of history
    assert "news_tone" not in fc.active_features(h)  # no news history -> news features off
    full_news = {t: (1.0, 0.0, 0) for t in h.times}
    assert "news_tone" in fc.active_features(_history(closes, full_news))


def test_scaler_caps_extremes():
    sc = fc.Scaler([0.0], [1.0])
    assert sc([10.0]) == [3.0] and sc([-10.0]) == [-3.0]


def _r(i: int, sentiment: int, up: bool, minutes: int) -> Reaction:
    item = NewsItem(id=i, title=f"t{i}", importance=6, sentiment=sentiment, category="crypto", source="s", url=f"u{i}",
                    title_hash=str(i), event_at=utcnow() - timedelta(minutes=minutes))
    return Reaction(item, 100.0, {240: 1.0 if up else -1.0})


def test_tone_check_compares_with_baseline():
    rs = [_r(1, -2, True, 0), _r(2, -2, False, 60), _r(3, 2, True, 120), _r(4, 0, True, 180)]
    t = fc.tone_check(rs)
    assert t.bearish_up == 0.5 and t.bearish_n == 2 and t.bullish_up == 1.0 and t.any_n == 4 and t.any_up == 0.75


@pytest.fixture
async def db():
    d = Database(":memory:")
    await d.init()
    yield d
    await d.close()


async def test_forecast_track_record(db):
    made = utcnow() - timedelta(hours=5)
    await db.add_forecast(made, 4, 0.7, 100.0, False)
    await db.add_forecast(made, 24, 0.4, 100.0, False)
    due = await db.due_forecasts()
    assert [r.horizon for r in due] == [4]  # 24 h one isn't due yet
    await db.resolve_forecast(due[0].id, 101.0)
    assert await db.forecast_record(4) == (1, 1, 1, 1)
    assert await db.due_forecasts() == []


def _fake_forecast() -> fc.Forecast:
    model = fc.Model(4, ["drawdown"], [0.0, 0.5], fc.Scaler([0.0], [1.0]), 2500, 0.49, 0.48, 540)
    o = fc.Outlook(4, 0.66, 73000.0, 75000.0, [fc.Driver("drawdown", "distance below the 7-day high", -0.08, 0.5)], model)
    return fc.Forecast(utcnow(), 74000.0, {4: o}, fc.ToneCheck(0.3, 10, 0.45, 53, 0.51, 591))


def test_forecast_line_marks_unproven_edge():
    line = forecast_line(_fake_forecast())
    assert "4h ⚪ 66% up" in line and "no proven edge" in line
    assert forecast_line(None) is None


async def test_forecast_screen(db):
    app = _app(db)

    fake = _fake_forecast()
    o = fake.outlooks[4]
    rm = vol.RangeModel(4, [0.0], False, 1.0, 2.0, [0.5, 1.0, 2.0], 0.69, 0.95, 900)
    o.range = vol.Range(o.low, o.high, 72000.0, 76000.0, 1.0, 0.31, rm)
    fake.context = [Signal("Macro", "VIX (stock-market fear)", "27.0", "high fear in stocks", -1)]

    class Engine:
        def __init__(self):
            self.forecasts = {"BTC": fake}

        def get(self, coin):
            return self.forecasts.get(coin)

        async def run(self, force=False, coin="BTC"):
            return self.forecasts.get(coin)

    app.forecast = Engine()  # type: ignore[assignment]
    text, kb = await forecast_screen(app)
    assert "BTC forecast" in text and "No reliable edge" in text and "leans up (66%)" in text
    assert "8.0% below" in text and "Does news tone predict the price?" in text and "45%" in text
    assert "19 in 20" in text and "over ±1%: <b>31%</b>" in text and "held the price <b>69%</b>" in text
    assert "Market context" in text and "VIX" in text
    for row in kb.inline_keyboard:
        for b in row:
            assert len(b.callback_data.encode()) <= 64
    text, _ = await forecast_screen(app, "eth")  # not trained yet
    assert "ETH forecast is being prepared" in text


def test_range_model_adapts_to_volatility_and_is_calibrated():
    # calm and wild regimes alternate every 10 days: ranges must widen in wild periods and hold ~68 % / 95 %
    rnd = random.Random(3)
    closes, times = [100.0], [0]
    for i in range(1, 6000):
        sigma = 0.002 if (i // 240) % 2 == 0 else 0.012
        closes.append(closes[-1] * math.exp(rnd.gauss(0, sigma)))
        times.append(i * HOUR)
    model = vol.train_range(closes, times, 4)
    assert model is not None and 0.6 < model.held68 < 0.76 and 0.9 < model.held95 < 0.99
    wild_end = 240 * 24 - 1  # ends inside block 23, which is wild ((5758 // 240) % 2 == 1)
    calm = vol.predict_range(model, closes[: 240 * 23 - 1], times[: 240 * 23 - 1])
    wild = vol.predict_range(model, closes[:wild_end], times[:wild_end])
    assert wild.high68 / wild.low68 > 2 * (calm.high68 / calm.low68 - 1) + 1  # wild range at least twice as wide
    assert wild.big_move_prob > calm.big_move_prob
    assert calm.low95 < calm.low68 < calm.high68 < calm.high95


async def test_forecast_record_is_per_coin(db):
    made = utcnow() - timedelta(hours=5)
    await db.add_forecast(made, 4, 0.7, 100.0, False, coin="ETH")
    [due] = await db.due_forecasts()
    assert due.coin == "ETH"
    await db.resolve_forecast(due.id, 99.0)
    assert await db.forecast_record(4, coin="ETH") == (1, 0, 1, 0)
    assert await db.forecast_record(4) == (0, 0, 0, 0)
