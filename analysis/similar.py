"""Learning from history: for a new event, find similar past events and show how BTC moved after them.

Similarity = 55 % headline words (rare words count more, TF-IDF style)
           + 35 % shared topics (Fed, Trump, war, hacks, …)
           + 10 % same category.

Past events within 6 hours of the new one are ignored (that is the *same* event, not history),
and events in the same 30-minute window are counted once, so one busy moment can't dominate.

The result is a *signal*, never a prediction: how often BTC rose afterwards, the typical move,
the range, how much bigger moves were than usual, and how confident that is given the sample size.
"""

from __future__ import annotations

import math
import statistics
import time
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime, timedelta

from analysis.patterns import WINDOW_MINUTES, _words, topics_of
from storage.db import Database, NewsItem, Reaction, utcnow

HORIZONS = (60, 240, 1440)
MIN_EVENTS = 5


@dataclass(frozen=True, slots=True)
class HorizonStats:
    n: int
    mean: float
    median: float
    up_share: float
    best: float
    worst: float
    mean_abs: float


@dataclass(frozen=True, slots=True)
class SimilarEvent:
    reaction: Reaction
    similarity: float


@dataclass
class Outlook:
    events: int
    stats: dict[int, HorizonStats]
    baseline: dict[int, HorizonStats]
    examples: list[SimilarEvent] = field(default_factory=list)

    @property
    def signal(self) -> str:
        s = self.stats.get(240)
        if s is None or s.n < MIN_EVENTS:
            return "⚪ Not enough history"
        if s.up_share >= 0.62 and s.median > 0.15:
            return "🟢 Bullish lean"
        if s.up_share <= 0.38 and s.median < -0.15:
            return "🔴 Bearish lean"
        return "⚪ No clear direction"

    @property
    def confidence(self) -> str:
        s = self.stats.get(240)
        if s is None or s.n < MIN_EVENTS:
            return "none"
        skew = abs(s.up_share - 0.5)
        if s.n >= 20 and skew >= 0.2:
            return "high"
        if s.n >= 10 and skew >= 0.12:
            return "medium"
        return "low"

    @property
    def volatility_ratio(self) -> float | None:
        """How much bigger the typical 4 h move was than after an average news item."""
        s, b = self.stats.get(240), self.baseline.get(240)
        if not s or not b or b.mean_abs == 0:
            return None
        return s.mean_abs / b.mean_abs


def horizon_stats(moves: list[float]) -> HorizonStats | None:
    if not moves:
        return None
    return HorizonStats(
        n=len(moves),
        mean=statistics.fmean(moves),
        median=statistics.median(moves),
        up_share=sum(1 for m in moves if m > 0) / len(moves),
        best=max(moves),
        worst=min(moves),
        mean_abs=statistics.fmean(abs(m) for m in moves),
    )


def _window(ts: datetime) -> tuple[int, int, int, int, int]:
    return (ts.year, ts.month, ts.day, ts.hour, ts.minute // WINDOW_MINUTES)


def dedupe_windows(reactions: list[Reaction]) -> list[Reaction]:
    """One reaction per 30-minute window (the first one given, i.e. the most similar)."""
    seen, out = set(), []
    for r in reactions:
        w = _window(r.item.event_at)
        if w not in seen:
            seen.add(w)
            out.append(r)
    return out


def summarize(reactions: list[Reaction]) -> dict[int, HorizonStats]:
    out = {}
    for h in HORIZONS:
        if s := horizon_stats([r.changes[h] for r in reactions if h in r.changes]):
            out[h] = s
    return out


@dataclass
class _Entry:
    reaction: Reaction
    words: frozenset[str]
    topics: frozenset[str]


class SimilarityEngine:
    """Keeps an in-memory index of past events with known BTC reactions (refreshed every 10 minutes)."""

    def __init__(self, db: Database, ttl: float = 600, days: int = 120):
        self.db, self.ttl, self.days = db, ttl, days
        self._built = 0.0
        self._entries: list[_Entry] = []
        self._idf: dict[str, float] = {}
        self._baseline: dict[int, HorizonStats] = {}

    def load(self, reactions: list[Reaction]) -> None:
        """Build the index from reactions (also used directly by tests)."""
        entries = [
            _Entry(r, _words(r.item.title), frozenset(topics_of(r.item)))
            for r in reactions
            if 240 in r.changes
        ]
        df = Counter(w for e in entries for w in e.words)
        n = max(1, len(entries))
        self._idf = {w: math.log(1 + n / c) for w, c in df.items()}
        self._entries = entries
        ordered = sorted((e.reaction for e in entries), key=lambda r: r.item.event_at)
        self._baseline = summarize(dedupe_windows(ordered))
        self._built = time.monotonic()

    async def refresh(self, force: bool = False) -> None:
        if force or time.monotonic() - self._built > self.ttl:
            self.load(await self.db.all_reactions(days=self.days))

    @property
    def size(self) -> int:
        return len(self._entries)

    def _idf_of(self, w: str) -> float:
        return self._idf.get(w, math.log(1 + max(1, len(self._entries))))  # unseen word = rare

    def _text_sim(self, a: frozenset[str], b: frozenset[str]) -> float:
        shared = a & b
        if not shared:
            return 0.0
        num = sum(self._idf_of(w) ** 2 for w in shared)
        den = math.sqrt(sum(self._idf_of(w) ** 2 for w in a)) * math.sqrt(sum(self._idf_of(w) ** 2 for w in b))
        return num / den if den else 0.0

    def rank(
        self,
        words: frozenset[str],
        topics: frozenset[str],
        category: str | None,
        *,
        exclude_near: datetime | None = None,
        exclude_item: NewsItem | None = None,
        limit: int = 40,
    ) -> list[SimilarEvent]:
        scored = []
        for e in self._entries:
            item = e.reaction.item
            if exclude_near and abs((item.event_at - exclude_near).total_seconds()) < 6 * 3600:
                continue
            text = self._text_sim(words, e.words)
            topic = len(topics & e.topics) / len(topics | e.topics) if topics and e.topics else 0.0
            if text < 0.12 and topic < 0.5:
                continue
            sim = 0.55 * text + 0.35 * topic + 0.10 * (1.0 if category and item.category == category else 0.0)
            if sim < 0.2:
                continue
            if exclude_item is not None and item.id == exclude_item.id:
                continue
            scored.append(SimilarEvent(e.reaction, sim))
        scored.sort(key=lambda s: -s.similarity)
        # keep the most similar event of each 30-minute window
        seen, out = set(), []
        for s in scored:
            w = _window(s.reaction.item.event_at)
            if w not in seen:
                seen.add(w)
                out.append(s)
            if len(out) >= limit:
                break
        return out

    def _outlook(self, similar: list[SimilarEvent]) -> Outlook | None:
        if not similar:
            return None
        reactions = [s.reaction for s in similar]
        return Outlook(events=len(reactions), stats=summarize(reactions), baseline=self._baseline, examples=similar)

    async def for_item(self, item: NewsItem, limit: int = 40) -> Outlook | None:
        await self.refresh()
        similar = self.rank(
            _words(item.title), frozenset(topics_of(item)), item.category,
            exclude_near=item.event_at, exclude_item=item, limit=limit,
        )
        return self._outlook(similar)

    async def for_text(self, text: str, limit: int = 40) -> Outlook | None:
        await self.refresh()
        probe = NewsItem(title=text, summary=None, category="", source="", url="", title_hash="", event_at=datetime.min)
        # a free-text question has no time; still skip events from the last 6 h (outcome not known yet)
        similar = self.rank(_words(text), frozenset(topics_of(probe)), None, limit=limit)
        cutoff = utcnow() - timedelta(hours=6)
        return self._outlook([s for s in similar if s.reaction.item.event_at < cutoff])
