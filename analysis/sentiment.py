"""Headline tone (bullish / bearish for crypto) and the composite Market Mood index.

Tone is read from the crypto-market point of view: a Fed rate cut, ETF inflows or a ceasefire are
"bullish" (risk-on), while hacks, rate hikes, tariffs or war escalation are "bearish" (risk-off).
Negations ("not hacked", "denies ban") cancel the signal instead of flipping it.
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from dataclasses import dataclass, field

from storage.db import NewsItem

_W = r"(?<![\w-])"  # word start
BULLISH: list[tuple[int, re.Pattern[str]]] = [
    (2, re.compile(_W + r"(all[- ]time high|record high|ath)\b", re.I)),
    (2, re.compile(_W + r"(approv\w+ .{0,30}etf|etf approv\w+|approves?)\b", re.I)),
    (2, re.compile(_W + r"(rate cuts?|cuts? (\w+ )?(interest |policy |key )?rates?|lowers? (\w+ )?rates?|easing|dovish|pivot)\b", re.I)),
    (2, re.compile(_W + r"(strategic bitcoin reserve|buys? (more )?bitcoin|adds? .{0,15}bitcoin|accumulat\w+|legal tender)\b", re.I)),
    (2, re.compile(_W + r"(shorts? (liquidated|squeezed)|short squeeze)\b", re.I)),
    (1, re.compile(_W + r"(surg\w+|soar\w*|rall(y|ies|ied)|jumps?|jumped|climbs?|gains?|rebounds?|rises?|rose|spikes?|breaks? above|tops?)\b", re.I)),
    (1, re.compile(_W + r"(inflows?|adoption|adopts?|partnership|upgrade|integrat\w+|listing|lists)\b", re.I)),
    (1, re.compile(_W + r"(ceasefire|peace (deal|talks)|truce|de-?escalat\w+|trade deal|agreement reached)\b", re.I)),
    (1, re.compile(_W + r"(inflation (cools|eases|slows|falls)|cooler[- ]than[- ]expected|soft landing)\b", re.I)),
    (1, re.compile(_W + r"(dismiss\w* (lawsuit|case|charges)|drops? (lawsuit|case|charges)|wins? (lawsuit|case)|clarity|bullish)\b", re.I)),
]
BEARISH: list[tuple[int, re.Pattern[str]]] = [
    (2, re.compile(_W + r"(crash\w*|plung\w+|collaps\w+|capitulat\w+|bloodbath)\b", re.I)),
    (2, re.compile(_W + r"(hack(ed|s)?|exploit(ed|s)?|drain(ed)?|stolen|rug ?pull|breach)\b", re.I)),
    (2, re.compile(_W + r"(rate hikes?|raises? (\w+ )?(interest |policy |key )?rates?|hikes? (\w+ )?rates?|hawkish|tightening)\b", re.I)),
    (2, re.compile(_W + r"(bans? (bitcoin|crypto\w*)|crackdown|halts? withdrawals|insolven\w+|bankrupt\w*|depeg\w*|delist\w*)\b", re.I)),
    (2, re.compile(_W + r"(longs? (liquidated|wiped)|liquidations?)\b", re.I)),
    (1, re.compile(_W + r"(tumbl\w+|slump\w*|slid(es)?|slides?|drops?|dropped|falls?|fell|sinks?|sank|declin\w+|dips?|sell-?off|loses?|lost|breaks? below|bearish)\b", re.I)),
    (1, re.compile(_W + r"(outflows?|sues?|sued|lawsuit|charg(es|ed)|indict\w+|fraud|probe|investigat\w+|fine[sd]?)\b", re.I)),
    (1, re.compile(_W + r"(war|missiles?|air ?strikes?|invasion|invad\w+|escalat\w+|attack(s|ed)?|troops)\b", re.I)),
    (1, re.compile(_W + r"(tariffs?|sanctions?|trade war|export controls?)\b", re.I)),
    (1, re.compile(_W + r"(hot(ter)?[- ]than[- ]expected|inflation (rises|jumps|accelerates|surges)|recession|stagflation|default)\b", re.I)),
]
_NEGATION = re.compile(r"\b(not|no|never|denies|denied|deny|without|fails? to|won't|isn't|aren't|wasn't|haven't|hasn't|rules? out)\W+(\w+\W+){0,2}$", re.I)

TONE_LABEL = {2: "🟢 bullish", 1: "🟢 slightly bullish", 0: "⚪ neutral", -1: "🔴 slightly bearish", -2: "🔴 bearish"}


def _hits(text: str, rules: list[tuple[int, re.Pattern[str]]]) -> int:
    total = 0
    for weight, rx in rules:
        for m in rx.finditer(text):
            if _NEGATION.search(text[: m.start()]):
                continue  # "has NOT been hacked" carries no bearish signal
            total += weight
            break  # each rule counts once per headline
    return total


# When debt, inflation, yields or unemployment hit a record or surge, that is bad news for risk assets.
_MACRO_STRESS = re.compile(
    r"\b(debt|deficit|inflation|cpi|yields?|unemployment|jobless|delinquenc\w+|layoffs?|oil prices?|dollar index|dxy)\b", re.I
)
_UP_MOVE = (0, 5)  # indexes of the "record high" and "surges/rises" rules in BULLISH


def headline_tone(title: str) -> int:
    """-2 (bearish) … +2 (bullish) for one headline."""
    bullish = _hits(title, BULLISH)
    bearish = _hits(title, BEARISH)
    if _MACRO_STRESS.search(title):
        up = _hits(title, [BULLISH[i] for i in _UP_MOVE])
        bullish -= up  # "credit card debt hits record high" is not bullish …
        bearish += min(up, 1)  # … it is mildly bearish for crypto
    return max(-2, min(2, bullish - bearish))


@dataclass(frozen=True, slots=True)
class NewsSentiment:
    index: float  # 0 = all bearish, 50 = balanced, 100 = all bullish (importance-weighted)
    bullish: int
    bearish: int
    neutral: int
    top_bullish: NewsItem | None = None
    top_bearish: NewsItem | None = None


def news_sentiment(items: Iterable[NewsItem]) -> NewsSentiment:
    """Importance-weighted balance of bullish vs bearish headlines."""
    pos = neg = 0.0
    counts = {1: 0, -1: 0, 0: 0}
    best_pos: tuple[float, NewsItem | None] = (0.0, None)
    best_neg: tuple[float, NewsItem | None] = (0.0, None)
    for n in items:
        tone = n.sentiment if n.sentiment is not None else headline_tone(n.title)
        weight = max(1, n.importance or 3) ** 1.5  # important news counts much more than noise
        counts[(tone > 0) - (tone < 0)] += 1
        if tone > 0:
            pos += tone * weight
            if tone * weight > best_pos[0]:
                best_pos = (tone * weight, n)
        elif tone < 0:
            neg += -tone * weight
            if -tone * weight > best_neg[0]:
                best_neg = (-tone * weight, n)
    index = 50.0 if pos + neg == 0 else 100 * pos / (pos + neg)
    return NewsSentiment(index, counts[1], counts[-1], counts[0], best_pos[1], best_neg[1])


def _clamp(x: float) -> float:
    return max(0.0, min(100.0, x))


@dataclass(frozen=True, slots=True)
class Mood:
    score: float  # 0 = extreme fear … 100 = extreme greed
    components: dict[str, float] = field(default_factory=dict)  # name -> 0..100

    @property
    def label(self) -> str:
        return mood_label(self.score)


def mood_label(score: float) -> str:
    if score < 25:
        return "Extreme fear"
    if score < 45:
        return "Fear"
    if score < 55:
        return "Neutral"
    if score < 75:
        return "Greed"
    return "Extreme greed"


def market_mood(
    *,
    fear_greed: float | None,
    news_index: float | None,
    btc_change_24h: float | None,
    btc_change_7d: float | None,
    funding_avg_pct: float | None,
    long_pct: float | None,
) -> Mood | None:
    """Blend of five transparent signals (missing ones are skipped and the weights re-normalised).

    - Fear & Greed index (35 %)                      — the market's own mood gauge
    - News tone (25 %)                               — bullish vs bearish headlines, weighted by importance
    - Price momentum (20 %)                          — BTC 24 h and 7 d change; ±10 % 7 d ≈ 0 / 100
    - Futures funding (10 %)                         — +0.03 %/8 h or more ≈ greedy longs
    - Retail positioning (10 %)                      — share of Binance accounts that are long
    """
    parts: dict[str, tuple[float, float]] = {}
    if fear_greed is not None:
        parts["Fear & Greed"] = (_clamp(fear_greed), 0.35)
    if news_index is not None:
        parts["News tone"] = (_clamp(news_index), 0.25)
    if btc_change_24h is not None or btc_change_7d is not None:
        momentum = 0.4 * (btc_change_24h or 0) * 2 + 0.6 * (btc_change_7d or 0)
        parts["Price momentum"] = (_clamp(50 + momentum * 5), 0.20)
    if funding_avg_pct is not None:
        parts["Funding"] = (_clamp(50 + funding_avg_pct / 0.03 * 40), 0.10)
    if long_pct is not None:
        parts["Positioning"] = (_clamp(50 + (long_pct - 0.5) * 150), 0.10)
    if not parts:
        return None
    total_w = sum(w for _, w in parts.values())
    score = sum(v * w for v, w in parts.values()) / total_w
    return Mood(score, {k: v for k, (v, _) in parts.items()})


SPARK = "▁▂▃▄▅▆▇█"


def sparkline(values: list[float], lo: float | None = None, hi: float | None = None) -> str:
    if not values:
        return ""
    lo = min(values) if lo is None else lo
    hi = max(values) if hi is None else hi
    span = (hi - lo) or 1
    return "".join(SPARK[min(7, max(0, int((v - lo) / span * 7.999)))] for v in values)


def gauge(score: float, width: int = 10) -> str:
    filled = round(_clamp(score) / 100 * width)
    return "▰" * filled + "▱" * (width - filled)
