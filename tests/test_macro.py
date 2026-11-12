"""Macro / flow data helpers and the market-context readings (no network)."""

import httpx

from analysis.market_context import market_context
from collectors.macro import DAY_MS, HOUR_MS, Klines, MacroClient, change_at, value_at


def test_value_at_has_no_look_ahead_and_expires():
    s = [(0, 1.0), (HOUR_MS, 2.0), (2 * HOUR_MS, 3.0)]
    assert value_at(s, 2 * HOUR_MS) == 2.0  # the bar opened at 2h hasn't closed by 2h
    assert value_at(s, 3 * HOUR_MS) == 3.0
    assert value_at(s, HOUR_MS - 1) is None
    assert value_at(s, 10 * DAY_MS) is None  # too old
    assert change_at(s, 3 * HOUR_MS, HOUR_MS) == 0.5


def _klines(n: int, buy_share: float) -> Klines:
    times = [i * HOUR_MS for i in range(n)]
    return Klines(times, [100.0] * n, [10.0] * n, [10.0 * buy_share] * n)


def test_market_context_readings():
    n = 24 * 10
    k = _klines(n, 0.56)
    t_end = n * HOUR_MS
    hourly = lambda f: [(i * HOUR_MS, f(i)) for i in range(n)]  # noqa: E731
    macro = {
        "spx": hourly(lambda i: 5000.0 if i < n - 24 else 5100.0),  # +2 % today
        "dxy": hourly(lambda i: 100.0),
        "vix": hourly(lambda i: 30.0),
        "us10y": hourly(lambda i: 4.0 if i < n - 100 else 4.3),
    }
    stables = [(d * DAY_MS, 100e9 * (1.01 if d >= 8 else 1.0)) for d in range(10)]
    premium = hourly(lambda i: 0.001)  # +0.1 %: crowded longs
    coinbase = hourly(lambda i: 100.1)  # +0.1 % over Binance
    ctx = {s.name: s for s in market_context("BTC", k, premium, coinbase, macro, stables, [(t_end - HOUR_MS, -0.0001)])}
    assert ctx["Market buys (24 h)"].vote == 1 and ctx["Market buys (24 h)"].value == "56.0%"
    assert ctx["Perp premium (8 h)"].vote == -1
    assert ctx["Funding rate"].vote == 1  # shorts pay longs
    assert ctx["Coinbase premium (24 h)"].vote == 1
    assert ctx["Stablecoin supply (7 d)"].vote == 1
    assert ctx["S&P 500 futures (24 h)"].vote == 1 and ctx["S&P 500 futures (24 h)"].value == "+2.00%"
    assert ctx["VIX (stock-market fear)"].vote == -1
    assert ctx["US 10-year yield"].vote == -1 and "+30 bp" in ctx["US 10-year yield"].value
    assert {s.group for s in ctx.values()} == {"Order flow", "Futures", "US demand", "Liquidity", "Macro"}
    assert market_context("BTC", None, [], [], {}, [], []) == []


async def test_klines_cache_grows_and_is_filtered():
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        start = int(request.url.params["startTime"])
        calls.append(start)
        rows = [[t, "1", "1", "1", str(t / HOUR_MS), "10", 0, "0", 0, "6", "0", "0"] for t in range(start, start + 5 * HOUR_MS, HOUR_MS)]
        return httpx.Response(200, json=rows)

    m = MacroClient(httpx.AsyncClient(transport=httpx.MockTransport(handler)))
    k = await m.klines("BTC", 0)
    assert k.times[:2] == [0, HOUR_MS] and k.taker_buy[0] == 6.0
    later = await m.klines("BTC", 3 * HOUR_MS)  # a shorter window is a filtered view of the cache
    assert later.times[0] == 3 * HOUR_MS
    assert (await m.klines("BTC", 0)).times[0] == 0  # the cache was not trimmed
    await m.close()
