"""Market context around a coin: order flow, futures, US demand, stablecoin liquidity and macro markets.

Each reading gets the usual trader's interpretation and a bullish / bearish / neutral vote. These are
*context*: in the bot's 2-year walk-forward test none of them predicted the next 4 h / 24 h direction better
than chance, so the forecast model doesn't use them (see analysis/forecast.py).
"""

from __future__ import annotations

from bisect import bisect_right

from analysis.technicals import Signal
from collectors.macro import DAY_MS, HOUR_MS, MACRO, Klines, Series, change_at, value_at


def _signed(v: float, digits: int = 2, unit: str = "%") -> str:
    return f"{v:+.{digits}f}{unit}".replace("-", "−")


def _avg(series: Series, t: int, hours: int) -> float | None:
    vals = [v for k in range(hours) if (v := value_at(series, t - k * HOUR_MS, max_age_ms=2 * HOUR_MS)) is not None]
    return sum(vals) / len(vals) if vals else None


def market_context(
    coin: str,
    k: Klines | None,
    premium: Series,
    coinbase: Series,
    macro: dict[str, Series],
    stables: Series,
    funding: list[tuple[int, float]],
) -> list[Signal]:
    out: list[Signal] = []
    add = out.append
    t = (k.times[-1] + HOUR_MS) if k and k.times else max((s[-1][0] + HOUR_MS for s in macro.values() if s), default=0)
    if not t:
        return out

    # Order flow: who is hitting the market (taker orders)
    if k and len(k.times) >= 24 * 8:
        v24, b24 = sum(k.volumes[-24:]), sum(k.taker_buy[-24:])
        if v24:
            share = b24 / v24 * 100
            add(Signal("Order flow", "Market buys (24 h)", f"{share:.1f}%",
                       "buyers more aggressive than sellers" if share > 52 else "sellers more aggressive than buyers" if share < 48
                       else "buyers and sellers balanced", 1 if share > 52 else -1 if share < 48 else 0))
        week = sum(k.volumes[-168:]) / 7
        if week:
            ratio = v24 / week
            add(Signal("Order flow", "Volume 24 h vs 7-day avg", f"{ratio:.1f}×",
                       "unusually busy — big players active" if ratio > 1.5 else "quiet — little conviction" if ratio < 0.7 else "normal", 0))

    # Futures
    if (b := _avg(premium, t, 8)) is not None:
        bp = b * 100
        add(Signal("Futures", "Perp premium (8 h)", _signed(bp, 3),
                   "futures well above spot — leveraged longs crowded, squeezes get likely" if bp > 0.05
                   else "futures below spot — shorts in control / fear" if bp < -0.01
                   else "normal", -1 if bp > 0.05 else 0))
    if funding:
        last_t, rate = funding[-1]
        if t - last_t < 2 * DAY_MS:
            r = rate * 100
            add(Signal("Futures", "Funding rate", _signed(r, 4),
                       "longs pay a lot — crowded long" if r > 0.03 else "shorts pay longs — crowded short (fuel for squeezes up)" if r < 0
                       else "normal", -1 if r > 0.03 else 1 if r < 0 else 0))

    # US demand: Coinbase (USD, mostly US buyers) vs Binance (USDT, global)
    if k and k.times and coinbase:
        diffs = []
        for j in range(1, 25):
            tt = k.times[-j]
            q = bisect_right(coinbase, (tt, float("inf"))) - 1
            if q >= 0 and coinbase[q][0] == tt:
                diffs.append(coinbase[q][1] / k.closes[-j] - 1)
        if diffs:
            p = sum(diffs) / len(diffs) * 100
            add(Signal("US demand", "Coinbase premium (24 h)", _signed(p, 3),
                       "US buyers pay more than the rest of the world — strong US demand" if p > 0.05
                       else "US investors selling / paying less" if p < -0.05 else "neutral", 1 if p > 0.05 else -1 if p < -0.05 else 0))

    # Liquidity
    if (s7 := change_at(stables, t, 7 * DAY_MS, bar_ms=DAY_MS)) is not None:
        s = s7 * 100
        add(Signal("Liquidity", "Stablecoin supply (7 d)", _signed(s, 2),
                   "new money arriving — buying power growing" if s > 0.5 else "money leaving crypto" if s < -0.3 else "flat",
                   1 if s > 0.5 else -1 if s < -0.3 else 0))

    # Macro markets (crypto trades as a risk asset)
    for name, risk_on in (("spx", True), ("ndx", True)):
        if (c := change_at(macro.get(name, []), t, DAY_MS)) is not None:
            v = c * 100
            add(Signal("Macro", MACRO[name][1] + " (24 h)", _signed(v, 2),
                       "stocks rising — risk-on helps crypto" if v > 0.5 else "stocks falling — risk-off weighs on crypto" if v < -0.5 else "quiet",
                       (1 if v > 0.5 else -1 if v < -0.5 else 0) if risk_on else 0))
    if (c := change_at(macro.get("dxy", []), t, DAY_MS)) is not None:
        v = c * 100
        add(Signal("Macro", "US dollar index (24 h)", _signed(v, 2),
                   "dollar strengthening — usually a headwind" if v > 0.3 else "dollar weakening — usually a tailwind" if v < -0.3 else "steady",
                   -1 if v > 0.3 else 1 if v < -0.3 else 0))
    if (vix := value_at(macro.get("vix", []), t, max_age_ms=5 * DAY_MS)) is not None:
        add(Signal("Macro", "VIX (stock-market fear)", f"{vix:.1f}",
                   "high fear in stocks — risk assets under pressure" if vix > 25 else "calm stock market" if vix < 15 else "normal",
                   -1 if vix > 25 else 1 if vix < 15 else 0))
    if (c := change_at(macro.get("gold", []), t, DAY_MS)) is not None:
        v = c * 100
        add(Signal("Macro", "Gold (24 h)", _signed(v, 2), "safe-haven demand" if v > 1 else "steady" if v > -1 else "safe havens sold", 0))
    us10 = macro.get("us10y", [])
    now, before = value_at(us10, t, max_age_ms=5 * DAY_MS), value_at(us10, t - 5 * DAY_MS, max_age_ms=5 * DAY_MS)
    if now is not None and before is not None:
        bp = (now - before) * 100
        add(Signal("Macro", "US 10-year yield", f"{now:.2f}% ({_signed(bp, 0, ' bp')} 5 d)",
                   "yields jumping — tighter money, a headwind" if bp > 15 else "yields falling — easier money, a tailwind" if bp < -15 else "steady",
                   -1 if bp > 15 else 1 if bp < -15 else 0))
    return out
