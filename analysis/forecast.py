"""BTC / ETH / SOL forecast for the next 4 h and 24 h: direction, and how far the price is likely to move.

How it works
------------
1. Every hour of the last ~120 days becomes one example with 10 features known at that moment:
   recent returns (4 h, 24 h, 7 d), volatility, distance from the 7-day high, Fear & Greed and its
   7-day change, futures funding, and the tone and volume of the news in the previous 6 hours.
2. The label is whether BTC was higher 4 h (or 24 h) later.
3. A logistic regression (a few weights, fitted with Newton's method in pure Python) learns how much
   each feature *actually* mattered. Nothing is assumed: if bearish headlines were followed by rises,
   the news-tone weight comes out small or even reversed.
4. Honesty check: the model is first trained on the older 80 % and tested on the most recent 20 % it
   never saw, against the simple baseline "always guess the more common direction". The screen shows
   both numbers; if the model doesn't beat the baseline, the forecast says there is no reliable edge.
5. The final model is refitted on all data and applied to the latest hour.
6. Price ranges come from a separate volatility model (analysis/volatility.py): the size of moves is far
   more predictable than their direction, and it is tested the same way.

A 2-year experiment (5 walk-forward folds, BTC/ETH/SOL) found that adding macro markets, futures basis,
Coinbase premium, taker flow and stablecoin supply did not improve direction accuracy (all within luck of
50 %), so those are shown as market context rather than fed to the direction model.

The output is a probability, not a promise.
"""

from __future__ import annotations

import asyncio
import logging
import math
import statistics
import time
from bisect import bisect_right
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

from analysis import technicals as ta
from analysis import volatility as vm
from analysis.market_context import market_context
from analysis.patterns import WINDOW_MINUTES
from collectors.exchanges import ExchangeHub
from collectors.fear_greed import fetch_history as fear_greed_history
from collectors.macro import MacroClient
from storage.db import Database, NewsItem

log = logging.getLogger(__name__)

HOUR_MS = 3_600_000
DAYS = 730  # two years of hourly prices: bull and bear phases, ~5 months of out-of-sample test
STEP = 4  # use every 4th hour as a training example (neighbouring hours add little new information)
HORIZONS = (4, 24)  # hours
COINS = ("BTC", "ETH", "SOL")
L2 = 50.0  # strong shrinkage: in testing it gave better-calibrated (less overconfident) probabilities
FEATURES: tuple[tuple[str, str], ...] = (
    ("ret_4h", "4-hour move"),
    ("ret_24h", "24-hour move"),
    ("ret_7d", "7-day trend"),
    ("vol_24h", "volatility"),
    ("drawdown", "distance below the 7-day high"),
    ("fg", "Fear & Greed"),
    ("fg_change", "Fear & Greed change (7 d)"),
    ("funding", "futures funding"),
    ("news_tone", "news tone (6 h)"),
    ("news_heat", "important news (6 h)"),
    # technical analysis (see analysis/technicals.py)
    ("dist_50d", "distance from the 50-day MA"),
    ("dist_200d", "distance from the 200-day MA"),
    ("dist_200w", "distance from the 200-week MA"),
    ("rsi_1h", "RSI 1h"),
    ("rsi_d", "RSI daily"),
    ("macd_d", "MACD daily"),
    ("bb_pos", "Bollinger band position"),
)
NAMES = [f for f, _ in FEATURES]
LABELS = dict(FEATURES)


# --- features ----------------------------------------------------------------------------


@dataclass
class History:
    times: list[int]  # hourly candle open times (ms), oldest first, only closed candles
    closes: list[float]
    fear_greed: dict[str, int]  # 'YYYY-MM-DD' -> value
    funding: list[tuple[int, float]]  # (ms, rate), sorted
    news: dict[int, tuple[float, float, int]]  # hour ms -> (bullish weight, bearish weight, important count)
    daily: list[list[float]] = field(default_factory=list)  # daily candles (open-time ms first), oldest first
    weekly: list[list[float]] = field(default_factory=list)  # weekly candles
    coin: str = "BTC"
    vix: list[tuple[int, float]] = field(default_factory=list)  # hourly VIX (for the range model)
    _ta: dict = field(default_factory=dict, repr=False)  # precomputed indicator series

    def ta(self) -> dict:
        """Indicator series computed once: daily SMA50/200, RSI, MACD histogram, Bollinger; weekly SMA200; hourly RSI."""
        if not self._ta:
            dc = [float(c[4]) for c in self.daily]
            wc = [float(c[4]) for c in self.weekly]
            _, hist = ta.macd(dc)
            self._ta = {
                "d_times": [int(c[0]) for c in self.daily],
                "sma50": ta.sma(dc, 50),
                "sma200": ta.sma(dc, 200),
                "rsi_d": ta.rsi(dc),
                "macd": hist,
                "bb": ta.bollinger(dc),
                "w_times": [int(c[0]) for c in self.weekly],
                "sma200w": ta.sma(wc, 200),
                "rsi_1h": ta.rsi(self.closes),
            }
        return self._ta


def _last_closed(times: list[int], length_ms: int, t: int) -> int:
    """Index of the last candle (of length_ms) that closed at or before t, or -1."""
    return bisect_right(times, t - length_ms) - 1


def news_by_hour(items: list[NewsItem]) -> dict[int, tuple[float, float, int]]:
    """Importance-weighted bullish/bearish weight and count of important (7+) items per hour."""
    out: dict[int, list[float]] = {}
    for n in items:
        if n.importance is None:
            continue
        ms = int(n.event_at.replace(tzinfo=UTC).timestamp() * 1000)
        hour = ms - ms % HOUR_MS
        b = out.setdefault(hour, [0.0, 0.0, 0])
        weight = max(1, n.importance) ** 1.5
        tone = n.sentiment or 0
        if tone > 0:
            b[0] += tone * weight
        elif tone < 0:
            b[1] += -tone * weight
        if n.importance >= 7:
            b[2] += 1
    return {h: (v[0], v[1], int(v[2])) for h, v in out.items()}


def _day(ms: int) -> str:
    return time.strftime("%Y-%m-%d", time.gmtime(ms / 1000))


def features_at(h: History, i: int) -> dict[str, float] | None:
    """Features for the candle at index i, using only information available at its close."""
    if i < 168:
        return None
    c = h.closes
    t = h.times[i] + HOUR_MS  # close time
    rets = [c[k] / c[k - 1] - 1 for k in range(i - 23, i + 1)]
    fg_now = h.fear_greed.get(_day(t))
    fg_week = h.fear_greed.get(_day(t - 7 * 24 * HOUR_MS))
    k = bisect_right(h.funding, (t, float("inf"))) - 1
    funding = h.funding[k][1] if k >= 0 and t - h.funding[k][0] < 24 * HOUR_MS else 0.0
    pos = neg = 0.0
    heat = 0
    for back in range(6):
        b = h.news.get(h.times[i] - back * HOUR_MS)
        if b:
            pos, neg, heat = pos + b[0], neg + b[1], heat + b[2]
    # Technicals: daily/weekly indicators from the last candle that had closed by time t (no look-ahead)
    s = h.ta()
    price = c[i]
    d = _last_closed(s["d_times"], 24 * HOUR_MS, t)
    w = _last_closed(s["w_times"], 7 * 24 * HOUR_MS, t)

    def rel(series: list, k: int) -> float:
        v = series[k] if 0 <= k < len(series) else None
        return price / v - 1 if v else 0.0

    bb = s["bb"][d] if 0 <= d < len(s["bb"]) else None
    macd_v = s["macd"][d] if 0 <= d < len(s["macd"]) else None
    rsi_d = s["rsi_d"][d] if 0 <= d < len(s["rsi_d"]) else None
    rsi_h = s["rsi_1h"][i]
    technical = {
        "dist_50d": rel(s["sma50"], d),
        "dist_200d": rel(s["sma200"], d),
        "dist_200w": rel(s["sma200w"], w),
        "rsi_1h": rsi_h if rsi_h is not None else 50.0,
        "rsi_d": rsi_d if rsi_d is not None else 50.0,
        "macd_d": (macd_v / price * 100) if macd_v is not None else 0.0,
        "bb_pos": (price - bb[0]) / (2 * bb[1]) if bb and bb[1] else 0.0,
    }
    return technical | {
        "ret_4h": c[i] / c[i - 4] - 1,
        "ret_24h": c[i] / c[i - 24] - 1,
        "ret_7d": c[i] / c[i - 168] - 1,
        "vol_24h": statistics.pstdev(rets),
        "drawdown": c[i] / max(c[i - 167 : i + 1]) - 1,
        "fg": float(fg_now if fg_now is not None else 50),
        "fg_change": float((fg_now - fg_week) if fg_now is not None and fg_week is not None else 0),
        "funding": funding * 100,  # in % per 8 h
        "news_tone": (pos - neg) / (pos + neg) if pos + neg > 0 else 0.0,
        "news_heat": math.log1p(heat),
    }


NEWS_FEATURES = ("news_tone", "news_heat")
MIN_NEWS_COVERAGE = 0.6  # news features need news in ≥ 60 % of training hours, else today's values look extreme


def active_features(h: History) -> list[str]:
    """Features with comparable history. News is skipped until the archive covers most hours."""
    if not h.times:
        return [n for n in NAMES if n not in NEWS_FEATURES]
    covered = sum(1 for t in h.times[168:] if any(t - k * HOUR_MS in h.news for k in range(6)))
    coverage = covered / max(1, len(h.times) - 168)
    return list(NAMES) if coverage >= MIN_NEWS_COVERAGE else [n for n in NAMES if n not in NEWS_FEATURES]


def dataset(h: History, horizon: int, names: list[str], step: int = STEP) -> tuple[list[list[float]], list[int]]:
    xs, ys = [], []
    for i in range(168, len(h.closes) - horizon, step):
        f = features_at(h, i)
        if f is not None:
            xs.append([f[n] for n in names])
            ys.append(1 if h.closes[i + horizon] > h.closes[i] else 0)
    return xs, ys


# --- logistic regression (Newton's method, pure Python) ----------------------------------


def _sigmoid(z: float) -> float:
    if z >= 0:
        return 1 / (1 + math.exp(-z))
    e = math.exp(z)
    return e / (1 + e)


def _solve(a: list[list[float]], b: list[float]) -> list[float]:
    """Gaussian elimination with partial pivoting for the small (≤ 11×11) Newton system."""
    n = len(b)
    m = [[*row[:], b[i]] for i, row in enumerate(a)]
    for col in range(n):
        piv = max(range(col, n), key=lambda r: abs(m[r][col]))
        m[col], m[piv] = m[piv], m[col]
        if abs(m[col][col]) < 1e-12:
            continue
        for r in range(n):
            if r != col:
                f = m[r][col] / m[col][col]
                if f:
                    m[r] = [x - f * y for x, y in zip(m[r], m[col], strict=True)]
    return [m[i][n] / m[i][i] if abs(m[i][i]) > 1e-12 else 0.0 for i in range(n)]


@dataclass
class Scaler:
    mean: list[float]
    std: list[float]

    @classmethod
    def fit(cls, xs: list[list[float]]) -> Scaler:
        cols = list(zip(*xs, strict=True))
        return cls([statistics.fmean(c) for c in cols], [statistics.pstdev(c) or 1.0 for c in cols])

    def __call__(self, x: list[float]) -> list[float]:
        # capped at ±3: one unusual reading must not dominate the forecast
        return [max(-3.0, min(3.0, (v - m) / s)) for v, m, s in zip(x, self.mean, self.std, strict=True)]


def fit_logistic(xs: list[list[float]], ys: list[int], l2: float = 5.0, iters: int = 30) -> list[float]:
    """Weights [bias, w1..wd] for standardized inputs. L2 keeps weights small (less overfitting)."""
    d = len(xs[0]) + 1
    rows = [[1.0, *x] for x in xs]
    w = [0.0] * d
    for _ in range(iters):
        grad = [0.0] * d
        hess = [[0.0] * d for _ in range(d)]
        for x, y in zip(rows, ys, strict=True):
            p = _sigmoid(sum(a * b for a, b in zip(w, x, strict=True)))
            err, s = p - y, p * (1 - p)
            for j in range(d):
                grad[j] += err * x[j]
                xj = s * x[j]
                row = hess[j]
                for k in range(j, d):
                    row[k] += xj * x[k]
        for j in range(d):
            for k in range(j):
                hess[j][k] = hess[k][j]
            if j:  # no penalty on the bias
                grad[j] += l2 * w[j]
                hess[j][j] += l2
        step = _solve(hess, grad)
        w = [a - b for a, b in zip(w, step, strict=True)]
        if max(abs(s) for s in step) < 1e-6:
            break
    return w


def predict(w: list[float], z: list[float]) -> float:
    return _sigmoid(w[0] + sum(a * b for a, b in zip(w[1:], z, strict=True)))


# --- model + forecast --------------------------------------------------------------------


@dataclass
class Model:
    horizon: int
    names: list[str]
    weights: list[float]
    scaler: Scaler
    samples: int
    test_acc: float  # accuracy on the most recent 20 % (unseen during that test fit)
    base_acc: float  # "always guess the more common direction" on the same data
    test_n: int

    @property
    def independent_tests(self) -> int:
        """Overlapping hourly windows aren't independent: ~one independent test per horizon length."""
        return max(1, min(self.test_n, self.test_n * STEP // self.horizon))

    @property
    def margin(self) -> float:
        """Accuracy needed above max(baseline, 50 %) to be more than luck (~2 standard errors)."""
        return 2 * math.sqrt(0.25 / self.independent_tests)

    @property
    def reliable(self) -> bool:
        return self.test_acc > max(self.base_acc, 0.5) + self.margin


def train(h: History, horizon: int) -> Model | None:
    names = active_features(h)
    xs, ys = dataset(h, horizon, names)
    if len(xs) < 300:
        return None
    split = int(len(xs) * 0.8)
    train_x, train_y = xs[: split - horizon], ys[: split - horizon]  # gap: no overlap between train labels and test
    test_x, test_y = xs[split:], ys[split:]
    sc = Scaler.fit(train_x)
    w = fit_logistic([sc(x) for x in train_x], train_y, l2=L2)
    hits = sum(1 for x, y in zip(test_x, test_y, strict=True) if (predict(w, sc(x)) > 0.5) == bool(y))
    majority = 1 if sum(train_y) * 2 >= len(train_y) else 0
    base = sum(1 for y in test_y if y == majority)
    full_sc = Scaler.fit(xs)
    full_w = fit_logistic([full_sc(x) for x in xs], ys, l2=L2)
    return Model(horizon, names, full_w, full_sc, len(xs), hits / len(test_y), base / len(test_y), len(test_y))


@dataclass
class Driver:
    name: str
    label: str
    value: float
    push: float  # contribution to the log-odds (+ = towards up)


@dataclass
class Outlook:
    horizon: int
    prob_up: float
    low: float
    high: float
    drivers: list[Driver]
    model: Model
    range: vm.Range | None = None  # from the volatility model (low/high above are its 68 % range)

    @property
    def verdict(self) -> str:
        if not self.model.reliable:
            return "⚪ No reliable edge"
        if self.prob_up >= 0.55:
            return "🟢 Up more likely"
        if self.prob_up <= 0.45:
            return "🔴 Down more likely"
        return "⚪ Close to a coin flip"


@dataclass
class ToneCheck:
    """Does headline tone predict the next 4 h? Share of rises after bullish / bearish news vs. any time."""

    bullish_up: float | None
    bullish_n: int
    bearish_up: float | None
    bearish_n: int
    any_up: float | None = None  # share of rises after any news (the baseline for the same period)
    any_n: int = 0


@dataclass
class Forecast:
    made_at: datetime
    price: float
    outlooks: dict[int, Outlook] = field(default_factory=dict)
    tone: ToneCheck | None = None
    coin: str = "BTC"
    context: list = field(default_factory=list)  # list[technicals.Signal]: macro, flow, derivatives


def outlook(model: Model, h: History, range_model: vm.RangeModel | None = None) -> Outlook | None:
    i = len(h.closes) - 1
    f = features_at(h, i)
    if f is None:
        return None
    z = model.scaler([f[n] for n in model.names])
    p = predict(model.weights, z)
    drivers = sorted(
        (Driver(n, LABELS[n], f[n], model.weights[k + 1] * z[k]) for k, n in enumerate(model.names)),
        key=lambda d: -abs(d.push),
    )
    rets = [h.closes[k] / h.closes[k - 1] - 1 for k in range(i - 71, i + 1)]
    sigma = statistics.pstdev(rets) * math.sqrt(model.horizon)  # ~68 % of moves fall within ±1 sigma
    price = h.closes[i]
    if range_model is not None:
        r = vm.predict_range(range_model, h.closes, h.times, h.vix)
        return Outlook(model.horizon, p, r.low68, r.high68, drivers, model, r)
    return Outlook(model.horizon, p, price * (1 - sigma), price * (1 + sigma), drivers, model)


def tone_check(reactions: list) -> ToneCheck:  # list[storage.db.Reaction]
    """Share of 30-minute windows where BTC was up 4 h after bullish / bearish important headlines."""

    def share(rs: list) -> tuple[float | None, int]:
        seen, ups = set(), []
        for r in sorted(rs, key=lambda r: -(r.item.importance or 0)):
            ts = r.item.event_at
            w = (ts.year, ts.month, ts.day, ts.hour, ts.minute // WINDOW_MINUTES)
            if w in seen or 240 not in r.changes:
                continue
            seen.add(w)
            ups.append(r.changes[240] > 0)
        return (sum(ups) / len(ups), len(ups)) if ups else (None, 0)

    important = [r for r in reactions if (r.item.importance or 0) >= 5 and r.item.sentiment]
    b_up, b_n = share([r for r in important if r.item.sentiment > 0])
    s_up, s_n = share([r for r in important if r.item.sentiment < 0])
    a_up, a_n = share(reactions)
    return ToneCheck(b_up, b_n, s_up, s_n, a_up, a_n)


def _train_all(h: History, horizon: int) -> tuple[Model | None, vm.RangeModel | None]:
    return train(h, horizon), vm.train_range(h.closes, h.times, horizon, h.vix)


class ForecastEngine:
    """Loads history, trains both horizons per coin and keeps the latest forecasts (refreshed at most every 15 min)."""

    def __init__(self, db: Database, hub: ExchangeHub, exchange: str = "binance", ttl: float = 900,
                 macro: MacroClient | None = None, coins: tuple[str, ...] = COINS):
        self.db, self.hub, self.exchange, self.ttl, self.macro, self.coins = db, hub, exchange, ttl, macro, coins
        self.forecasts: dict[str, Forecast] = {}
        self._at: dict[str, float] = {}
        self._locks: dict[str, asyncio.Lock] = {}

    @property
    def latest(self) -> Forecast | None:
        """The BTC forecast (Home, digest)."""
        return self.forecasts.get("BTC")

    def get(self, coin: str) -> Forecast | None:
        return self.forecasts.get(coin.upper())

    async def _hourly(self, coin: str, since: int, now_ms: int) -> tuple[list[int], list[float]]:
        if self.macro is not None:
            try:
                k = await self.macro.klines(coin, since)
                if len(k.times) > 400:
                    return k.times, k.closes
            except Exception as exc:
                log.warning("forecast: Binance klines for %s unavailable, using the exchange hub: %s", coin, exc)
        candles: list[list[float]] = []
        cursor = since
        while cursor < now_ms:
            batch = await self.hub.candles(self.exchange, coin, cursor, 1000, timeframe="1h")
            if not batch:
                break
            candles += batch
            cursor = int(batch[-1][0]) + HOUR_MS
            if len(batch) < 1000:
                break
        candles = [c for c in candles if int(c[0]) + HOUR_MS <= now_ms]  # closed candles only
        return [int(c[0]) for c in candles], [float(c[4]) for c in candles]

    async def load_history(self, coin: str = "BTC") -> History:
        now_ms = int(time.time() * 1000)
        since = now_ms - DAYS * 24 * HOUR_MS
        times, closes = await self._hourly(coin, since, now_ms)
        try:
            funding = await self.hub.funding_history(self.exchange, coin, since)
        except Exception as exc:  # funding is one feature among many
            log.warning("forecast: funding history unavailable: %s", exc)
            funding = []
        # daily candles back far enough for a 200-day MA at the start of the window; weekly for the 200-week MA
        daily = await self.hub.candles(self.exchange, coin, since - 230 * 24 * HOUR_MS, 1000, timeframe="1d")
        weekly = await self.hub.candles(self.exchange, coin, since - 1450 * 24 * HOUR_MS, 1000, timeframe="1w")
        fg = await fear_greed_history(DAYS + 10)
        vix: list[tuple[int, float]] = []
        if self.macro is not None:
            try:
                vix = await self.macro.yahoo("vix", since)
            except Exception as exc:
                log.warning("forecast: VIX unavailable: %s", exc)
        news: dict = {}
        if coin == "BTC":  # news reactions are recorded against BTC
            start = datetime.now(UTC).replace(tzinfo=None) - timedelta(days=DAYS)
            news = news_by_hour(await self.db.news_between(start, datetime.now(UTC).replace(tzinfo=None)))
        return History(times=times, closes=closes, fear_greed=fg, funding=sorted(funding), news=news,
                       daily=daily, weekly=weekly, coin=coin, vix=vix)

    async def _context(self, coin: str, h: History) -> list:
        if self.macro is None:
            return []
        since = h.times[-1] - 60 * 24 * HOUR_MS if h.times else int(time.time() * 1000) - 60 * 24 * HOUR_MS
        got = await asyncio.gather(
            self.macro.klines(coin, h.times[0] if h.times else since), self.macro.premium(coin, since),
            self.macro.coinbase(coin, since), self.macro.macro_all(since), self.macro.stablecoins(since),
            return_exceptions=True,
        )
        for g in got:
            if isinstance(g, BaseException):
                log.warning("forecast: some market context for %s unavailable: %s", coin, g)
        k, prem, cb, mac, stables = (None if isinstance(g, BaseException) else g for g in got)
        try:
            return market_context(coin, k, prem or [], cb or [], mac or {}, stables or [], h.funding)
        except Exception:  # context is extra information; never block the forecast
            log.exception("forecast: market context for %s failed", coin)
            return []

    async def run(self, force: bool = False, coin: str = "BTC") -> Forecast | None:
        coin = coin.upper()
        async with self._locks.setdefault(coin, asyncio.Lock()):
            cached = self.forecasts.get(coin)
            if not force and cached is not None and time.monotonic() - self._at.get(coin, 0) < self.ttl:
                return cached
            h = await self.load_history(coin)
            if len(h.closes) < 400:
                log.warning("forecast: not enough price history for %s (%d hours)", coin, len(h.closes))
                return cached
            fc = Forecast(made_at=datetime.now(UTC).replace(tzinfo=None), price=h.closes[-1], coin=coin)
            for hz in HORIZONS:
                # CPU-heavy: train in a thread so the bot keeps answering while it runs
                model, rmodel = await asyncio.to_thread(_train_all, h, hz)
                if model and (o := outlook(model, h, rmodel)):
                    fc.outlooks[hz] = o
                    log.info("forecast %s %dh: P(up)=%.2f test acc %.3f vs base %.3f (n=%d); range held %s",
                             coin, hz, o.prob_up, model.test_acc, model.base_acc, model.test_n,
                             f"{rmodel.held68:.0%}/{rmodel.held95:.0%}" if rmodel else "n/a")
            if coin == "BTC":
                fc.tone = tone_check(await self.db.all_reactions(days=DAYS))
            fc.context = await self._context(coin, h)
            self.forecasts[coin], self._at[coin] = fc, time.monotonic()
            return fc

    async def run_all(self, force: bool = False) -> dict[str, Forecast]:
        for coin in self.coins:  # one at a time: training is CPU-heavy
            try:
                await self.run(force, coin)
            except Exception:
                log.exception("forecast for %s failed", coin)
        return dict(self.forecasts)
