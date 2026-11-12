"""How big will the next move be? A volatility model for the forecast's price ranges.

Direction is close to unpredictable; the *size* of moves is not: quiet hours follow quiet hours, wild follow
wild, weekends are calmer, and stock-market fear (VIX) spills into crypto. The model regresses the log of the
next move's size on recent realized volatility over 4 h, 24 h, 7 d and 30 d (a "HAR" model), the last day's
move, the time of day / week and the VIX. Ranges are scaled so that, on past data, the 68 % range held the
price 68 % of the time and the 95 % range 95 % of the time. The share actually held on the most recent
40 % of history (data the model never saw) is reported next to every range.
"""

from __future__ import annotations

import math
from bisect import bisect_right
from dataclasses import dataclass

HOUR_MS = 3_600_000
DAY_MS = 24 * HOUR_MS
MIN_HISTORY = 720  # 30 days of hourly closes
STEP = 2
BIG_MOVE = {4: 1.0, 24: 3.0}  # % that counts as a "big move" per horizon


def _quantile(sorted_values: list[float], q: float) -> float:
    if not sorted_values:
        return 1.0
    pos = q * (len(sorted_values) - 1)
    lo = int(pos)
    hi = min(lo + 1, len(sorted_values) - 1)
    return sorted_values[lo] + (sorted_values[hi] - sorted_values[lo]) * (pos - lo)


def _lstsq(xs: list[list[float]], ys: list[float], ridge: float = 1e-3) -> list[float]:
    from analysis.forecast import _solve

    d = len(xs[0]) + 1
    ata = [[0.0] * d for _ in range(d)]
    aty = [0.0] * d
    for x, y in zip(xs, ys, strict=True):
        row = [1.0, *x]
        for j in range(d):
            aty[j] += row[j] * y
            rj = row[j]
            a = ata[j]
            for k in range(j, d):
                a[k] += rj * row[k]
    for j in range(d):
        for k in range(j):
            ata[j][k] = ata[k][j]
        ata[j][j] += ridge
    return _solve(ata, aty)


class _Realized:
    """Prefix sums of squared hourly log returns: realized volatility over any window in O(1)."""

    def __init__(self, closes: list[float]):
        self.closes = closes
        self.cum = [0.0]
        prev = closes[0] if closes else 1.0
        for c in closes:
            r = math.log(c / prev) if prev > 0 and c > 0 else 0.0
            self.cum.append(self.cum[-1] + r * r)
            prev = c

    def vol(self, i: int, n: int) -> float:
        """Hourly volatility over the n hours ending at index i."""
        return math.sqrt(max(0.0, self.cum[i + 1] - self.cum[i + 1 - n]) / n)


def _vix_at(vix: list[tuple[int, float]], t: int) -> float | None:
    k = bisect_right(vix, (t - HOUR_MS, float("inf"))) - 1
    return vix[k][1] if k >= 0 and t - vix[k][0] <= 5 * DAY_MS else None


def features(rv: _Realized, times: list[int], i: int, vix: list[tuple[int, float]] | None) -> list[float]:
    t = times[i] + HOUR_MS
    hour = (t // HOUR_MS) % 24
    weekday = (t // DAY_MS + 3) % 7  # 1970-01-01 was a Thursday; 0 = Monday
    c = rv.closes
    out = [
        math.log(rv.vol(i, 4) + 1e-6),
        math.log(rv.vol(i, 24) + 1e-6),
        math.log(rv.vol(i, 168) + 1e-6),
        math.log(rv.vol(i, 720) + 1e-6),
        abs(math.log(c[i] / c[i - 24])),
        math.sin(2 * math.pi * hour / 24),
        math.cos(2 * math.pi * hour / 24),
        1.0 if weekday >= 5 else 0.0,
    ]
    if vix is not None:
        out.append(math.log(_vix_at(vix, t) or 18.0))
    return out


@dataclass
class RangeModel:
    horizon: int
    weights: list[float]
    use_vix: bool
    k68: float  # multiplier: |move| <= k68 * predicted size in 68 % of past cases
    k95: float
    ratios: list[float]  # sorted |move| / predicted size on all history (for big-move odds)
    held68: float  # share of unseen test cases inside the 68 % range
    held95: float
    test_n: int

    def size(self, x: list[float]) -> float:
        return math.exp(self.weights[0] + sum(w * v for w, v in zip(self.weights[1:], x, strict=True)))


@dataclass
class Range:
    low68: float
    high68: float
    low95: float
    high95: float
    big_move_pct: float  # threshold, e.g. 3.0 for ±3 %
    big_move_prob: float  # chance the price ends further than that from now
    model: RangeModel


def _dataset(closes: list[float], times: list[int], horizon: int, vix) -> tuple[list[list[float]], list[float]]:  # type: ignore[no-untyped-def]
    rv = _Realized(closes)
    xs, moves = [], []
    for i in range(MIN_HISTORY, len(closes) - horizon, STEP):
        xs.append(features(rv, times, i, vix))
        moves.append(abs(math.log(closes[i + horizon] / closes[i])))
    return xs, moves


def _fit(xs: list[list[float]], moves: list[float]) -> tuple[list[float], list[float]]:
    w = _lstsq(xs, [math.log(m + 1e-5) for m in moves])
    preds = [math.exp(w[0] + sum(a * b for a, b in zip(w[1:], x, strict=True))) for x in xs]
    return w, sorted(m / p for m, p in zip(moves, preds, strict=True))


def train_range(closes: list[float], times: list[int], horizon: int, vix: list[tuple[int, float]] | None = None) -> RangeModel | None:
    use_vix = bool(vix) and len(vix or []) > 1000  # type: ignore[arg-type]
    xs, moves = _dataset(closes, times, horizon, vix if use_vix else None)
    if len(xs) < 500:
        return None
    split = int(len(xs) * 0.6)
    gap = horizon // STEP + 1
    w, ratios = _fit(xs[: split - gap], moves[: split - gap])
    k68, k95 = _quantile(ratios, 0.68), _quantile(ratios, 0.95)
    held68 = held95 = 0
    for x, m in zip(xs[split:], moves[split:], strict=True):
        size = math.exp(w[0] + sum(a * b for a, b in zip(w[1:], x, strict=True)))
        held68 += m <= k68 * size
        held95 += m <= k95 * size
    n = len(xs) - split
    w_all, ratios_all = _fit(xs, moves)
    return RangeModel(horizon, w_all, use_vix, _quantile(ratios_all, 0.68), _quantile(ratios_all, 0.95), ratios_all,
                      held68 / n, held95 / n, n)


def predict_range(model: RangeModel, closes: list[float], times: list[int], vix: list[tuple[int, float]] | None = None) -> Range:
    i = len(closes) - 1
    size = model.size(features(_Realized(closes), times, i, vix if model.use_vix else None))
    price = closes[-1]
    threshold = BIG_MOVE.get(model.horizon, 2.0)
    cut = math.log(1 + threshold / 100) / size  # a move this many "sizes" big crosses the threshold
    beyond = len(model.ratios) - bisect_right(model.ratios, cut)
    return Range(
        price * math.exp(-model.k68 * size), price * math.exp(model.k68 * size),
        price * math.exp(-model.k95 * size), price * math.exp(model.k95 * size),
        threshold, beyond / len(model.ratios), model,
    )
