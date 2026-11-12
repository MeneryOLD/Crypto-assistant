"""Technical analysis: the indicators traders watch, computed from exchange candles (pure Python).

Trend      SMA 20/50/200-day, EMA 21-day, 200-week SMA (long-cycle floor), golden/death cross, Mayer multiple
Momentum   RSI 14 (1 h, 4 h, daily), MACD 12/26/9 (daily)
Volatility Bollinger Bands 20/2 (position, width/squeeze), ATR 14 (daily, % of price)
Levels     support / resistance (30-day swing low / high), 52-week high / low, distance to each
Volume     today's volume vs. its 20-day average

Every reading comes with a plain-language interpretation and a bullish / bearish / neutral vote.
"""

from __future__ import annotations

import asyncio
import statistics
from dataclasses import dataclass, field

from collectors.exchanges import ExchangeHub

DAY_MS = 86_400_000


# --- indicator math (lists aligned with the input; None where not enough data) -------------


def sma(values: list[float], n: int) -> list[float | None]:
    out: list[float | None] = [None] * len(values)
    total = 0.0
    for i, v in enumerate(values):
        total += v
        if i >= n:
            total -= values[i - n]
        if i >= n - 1:
            out[i] = total / n
    return out


def ema(values: list[float], n: int) -> list[float | None]:
    out: list[float | None] = [None] * len(values)
    if len(values) < n:
        return out
    k = 2 / (n + 1)
    prev = sum(values[:n]) / n
    out[n - 1] = prev
    for i in range(n, len(values)):
        prev = values[i] * k + prev * (1 - k)
        out[i] = prev
    return out


def rsi(values: list[float], n: int = 14) -> list[float | None]:
    """Wilder's RSI."""
    out: list[float | None] = [None] * len(values)
    if len(values) <= n:
        return out
    gains = [max(0.0, values[i] - values[i - 1]) for i in range(1, len(values))]
    losses = [max(0.0, values[i - 1] - values[i]) for i in range(1, len(values))]
    avg_g, avg_l = sum(gains[:n]) / n, sum(losses[:n]) / n
    for i in range(n, len(values)):
        if i > n:
            avg_g = (avg_g * (n - 1) + gains[i - 1]) / n
            avg_l = (avg_l * (n - 1) + losses[i - 1]) / n
        out[i] = 100.0 if avg_l == 0 else 100 - 100 / (1 + avg_g / avg_l)
    return out


def macd(values: list[float], fast: int = 12, slow: int = 26, signal: int = 9) -> tuple[list[float | None], list[float | None]]:
    """(MACD line, histogram)."""
    ef, es = ema(values, fast), ema(values, slow)
    line = [f - s if f is not None and s is not None else None for f, s in zip(ef, es, strict=True)]
    valid = [v for v in line if v is not None]
    sig_valid = ema(valid, signal)
    sig: list[float | None] = [None] * (len(line) - len(valid)) + sig_valid
    hist = [m - s if m is not None and s is not None else None for m, s in zip(line, sig, strict=True)]
    return line, hist


def bollinger(values: list[float], n: int = 20, k: float = 2.0) -> list[tuple[float, float] | None]:
    """(middle, standard deviation) per point."""
    out: list[tuple[float, float] | None] = [None] * len(values)
    for i in range(n - 1, len(values)):
        window = values[i - n + 1 : i + 1]
        out[i] = (sum(window) / n, statistics.pstdev(window))
    return out


def atr(highs: list[float], lows: list[float], closes: list[float], n: int = 14) -> list[float | None]:
    trs = [highs[0] - lows[0]] + [
        max(highs[i] - lows[i], abs(highs[i] - closes[i - 1]), abs(lows[i] - closes[i - 1])) for i in range(1, len(closes))
    ]
    out: list[float | None] = [None] * len(closes)
    if len(trs) < n:
        return out
    prev = sum(trs[:n]) / n
    out[n - 1] = prev
    for i in range(n, len(trs)):
        prev = (prev * (n - 1) + trs[i]) / n
        out[i] = prev
    return out


# --- snapshot with interpretation ------------------------------------------------------------


@dataclass
class Signal:
    group: str  # Trend | Momentum | Volatility | Levels | Volume
    name: str
    value: str
    meaning: str
    vote: int  # +1 bullish, -1 bearish, 0 neutral


@dataclass
class Technicals:
    coin: str
    price: float
    signals: list[Signal] = field(default_factory=list)

    @property
    def score(self) -> tuple[int, int, int]:
        """(bullish, bearish, neutral) votes."""
        return (sum(1 for s in self.signals if s.vote > 0), sum(1 for s in self.signals if s.vote < 0),
                sum(1 for s in self.signals if s.vote == 0))

    @property
    def verdict(self) -> str:
        bull, bear, _ = self.score
        if bull >= bear + 3:
            return "🟢 Mostly bullish"
        if bear >= bull + 3:
            return "🔴 Mostly bearish"
        return "⚪ Mixed"


def _pct(a: float, b: float) -> float:
    return (a / b - 1) * 100


def analyse(coin: str, daily: list[list[float]], weekly: list[list[float]], h4: list[list[float]], h1: list[list[float]]) -> Technicals:
    """Candles are [ts, open, high, low, close, volume]; the last one may be still open (it is the live price)."""
    closes = [float(c[4]) for c in daily]
    highs, lows, vols = [float(c[2]) for c in daily], [float(c[3]) for c in daily], [float(c[5]) for c in daily]
    price = closes[-1]
    t = Technicals(coin, price)
    add = t.signals.append

    # Trend
    s20, s50, s200 = sma(closes, 20)[-1], sma(closes, 50)[-1], sma(closes, 200)[-1]
    for n, v in ((20, s20), (50, s50), (200, s200)):
        if v:
            d = _pct(price, v)
            add(Signal("Trend", f"{n}-day MA", f"${v:,.0f} ({d:+.1f}%)".replace("-", "−"),
                       f"price {'above' if d > 0 else 'below'} — {'short' if n == 20 else 'medium' if n == 50 else 'long'}-term trend {'up' if d > 0 else 'down'}",
                       1 if d > 0 else -1))
    wcloses = [float(c[4]) for c in weekly]
    w200 = sma(wcloses, 200)[-1] if len(wcloses) >= 200 else None
    if w200:
        d = _pct(price, w200)
        meaning = ("below the 200-week MA — rare; historically deep-value / capitulation zone" if d < 0
                   else "just above the 200-week MA — historically a cycle-bottom zone" if d < 15
                   else "far above the 200-week MA — late-cycle territory" if d > 150
                   else "well above the 200-week MA — long-term uptrend intact")
        add(Signal("Trend", "200-week MA", f"${w200:,.0f} ({d:+.0f}%)".replace("-", "−"), meaning, 1 if d < 15 else (-1 if d > 150 else 0)))
    if s50 and s200:
        s50_prev, s200_prev = sma(closes, 50)[-11], sma(closes, 200)[-11]
        golden = s50 > s200
        recent = s50_prev is not None and s200_prev is not None and (s50_prev > s200_prev) != golden
        add(Signal("Trend", "50/200 cross", "golden cross" if golden else "death cross",
                   ("just happened (last 10 days) — " if recent else "") + ("50-day above 200-day: bull structure" if golden else "50-day below 200-day: bear structure"),
                   1 if golden else -1))
        mayer = price / s200
        add(Signal("Trend", "Mayer multiple", f"{mayer:.2f}",
                   "below 0.8 — historically deep value" if mayer < 0.8 else "above 2.4 — historically overheated" if mayer > 2.4 else "normal range (0.8–2.4)",
                   1 if mayer < 0.8 else (-1 if mayer > 2.4 else 0)))

    # Momentum
    for label, series in (("RSI 1h", [float(c[4]) for c in h1]), ("RSI 4h", [float(c[4]) for c in h4]), ("RSI daily", closes)):
        r = rsi(series)[-1] if len(series) > 15 else None
        if r is not None:
            meaning = "overbought (>70) — rallies often pause" if r > 70 else "oversold (<30) — bounces are common" if r < 30 else (
                "bullish momentum" if r > 55 else "bearish momentum" if r < 45 else "neutral")
            add(Signal("Momentum", label, f"{r:.0f}", meaning, -1 if r > 70 else 1 if r < 30 else (1 if r > 55 else -1 if r < 45 else 0)))
    _line, hist = macd(closes)
    if hist[-1] is not None and hist[-2] is not None:
        crossed = (hist[-1] > 0) != (hist[-2] > 0)
        add(Signal("Momentum", "MACD (daily)", f"{hist[-1]:+,.0f}".replace("-", "−"),
                   ("just crossed " if crossed else "") + ("above signal — momentum improving" if hist[-1] > 0 else "below signal — momentum weakening"),
                   1 if hist[-1] > 0 else -1))

    # Volatility
    bb = bollinger(closes)[-1]
    if bb:
        mid, sd = bb
        pos = (price - mid) / (2 * sd) if sd else 0
        width = 4 * sd / mid * 100
        widths = [4 * b[1] / b[0] * 100 for b in bollinger(closes)[-120:] if b]
        squeeze = widths and width <= sorted(widths)[len(widths) // 5]
        meaning = ("at the upper band — stretched up" if pos > 0.9 else "at the lower band — stretched down" if pos < -0.9 else "inside the bands")
        if squeeze:
            meaning += "; bands unusually tight — a big move often follows"
        add(Signal("Volatility", "Bollinger 20/2", f"{pos:+.2f} (width {width:.1f}%)".replace("-", "−"), meaning,
                   -1 if pos > 0.9 else 1 if pos < -0.9 else 0))
    a = atr(highs, lows, closes)[-1]
    if a:
        add(Signal("Volatility", "ATR 14 (daily)", f"${a:,.0f} ({a / price * 100:.1f}% of price)", "typical daily range", 0))

    # Levels
    support, resistance = min(lows[-31:-1]), max(highs[-31:-1])
    add(Signal("Levels", "30-day support", f"${support:,.0f} ({_pct(price, support):+.1f}%)".replace("-", "−"),
               "price is testing support" if _pct(price, support) < 2 else "distance to the recent low", 0))
    add(Signal("Levels", "30-day resistance", f"${resistance:,.0f} ({_pct(price, resistance):+.1f}%)".replace("-", "−"),
               "breaking out above resistance" if price > resistance else "price is testing resistance" if _pct(price, resistance) > -2 else "distance to the recent high",
               1 if price > resistance else 0))
    year_hi, year_lo = max(highs[-365:]), min(lows[-365:])
    from_hi = _pct(price, year_hi)
    add(Signal("Levels", "52-week range", f"${year_lo:,.0f} – ${year_hi:,.0f}",
               f"{abs(from_hi):.0f}% below the yearly high" if from_hi < -1 else "at the yearly high",
               -1 if from_hi < -30 else 0))

    # Volume
    if len(vols) > 22:
        avg = sum(vols[-22:-2]) / 20
        ratio = vols[-2] / avg if avg else 1  # yesterday: today's candle has only just started
        add(Signal("Volume", "Volume yesterday vs 20-day avg", f"{ratio:.1f}×",
                   "heavy volume — the move has conviction" if ratio > 1.5 else "light volume — weak participation" if ratio < 0.6 else "normal", 0))
    return t


async def technicals(hub: ExchangeHub, coin: str = "BTC", exchange: str | None = None) -> Technicals:
    import time

    ex = exchange or hub.names[0]
    now = int(time.time() * 1000)
    daily, weekly, h4, h1 = await asyncio.gather(
        hub.candles(ex, coin, now - 400 * DAY_MS, 1000, timeframe="1d"),
        hub.candles(ex, coin, now - 1500 * DAY_MS, 1000, timeframe="1w"),
        hub.candles(ex, coin, now - 60 * DAY_MS, 1000, timeframe="4h"),
        hub.candles(ex, coin, now - 5 * DAY_MS, 1000, timeframe="1h"),
    )
    return analyse(coin.upper(), daily, weekly, h4, h1)
