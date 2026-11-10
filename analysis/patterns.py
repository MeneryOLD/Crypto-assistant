"""Learning from history: how BTC moved, on average, after different kinds of news.

Many articles are published around the same moment and share the same price move, so events are
grouped into 30-minute windows first; each window counts once. Otherwise one busy hour would dominate.
"""

from __future__ import annotations

import re
import time
from dataclasses import dataclass
from datetime import datetime
from functools import lru_cache

from storage.db import Database, NewsItem, Reaction

WINDOW_MINUTES = 30

TOPICS: dict[str, re.Pattern[str]] = {
    "🏦 Fed & rates": re.compile(r"\b(fed|federal reserve|fomc|powell|rate (?:cut|hike)s?|interest rates?)\b", re.I),
    "📈 Inflation & jobs": re.compile(r"\b(cpi|inflation|pce|payrolls?|jobs report|unemployment|jobless)\b", re.I),
    "⚖️ SEC & regulation": re.compile(r"\b(sec|cftc|regulat\w*|lawsuit|enforcement|gensler|atkins)\b", re.I),
    "💼 ETFs & funds": re.compile(r"\b(etfs?|blackrock|ibit|fidelity|grayscale|inflows?|outflows?)\b", re.I),
    "🇺🇸 Trump": re.compile(r"\btrump\b", re.I),
    "🗳 Elections": re.compile(r"\b(elections?|voters?|ballots?|referendum|midterms?)\b", re.I),
    "💥 War & conflict": re.compile(r"\b(war|missiles?|strikes?|invasion|ceasefire|troops|attack(?:s|ed)?|drones?)\b", re.I),
    "🚢 Tariffs & sanctions": re.compile(r"\b(tariffs?|trade war|sanctions?|export controls?)\b", re.I),
    "🇨🇳 China": re.compile(r"\b(china|chinese|beijing|xi jinping|pboc)\b", re.I),
    "🛢 Oil & energy": re.compile(r"\b(oil|opec|crude|brent|natural gas)\b", re.I),
    "🪙 Stablecoins": re.compile(r"\b(stablecoins?|tether|usdt|usdc|circle)\b", re.I),
    "🔓 Hacks & exploits": re.compile(r"\b(hack(?:ed|s)?|exploit(?:ed)?|drained|stolen)\b", re.I),
    "🐋 Whales & treasuries": re.compile(r"\b(whales?|microstrategy|saylor|strategy buys|treasury compan\w+)\b", re.I),
}


@dataclass(frozen=True, slots=True)
class Pattern:
    name: str
    events: int  # distinct 30-minute windows
    avg: dict[int, float]  # minutes -> average % move
    up_share: dict[int, float]  # minutes -> share of windows where BTC went up
    samples: dict[int, int]  # minutes -> windows that have this horizon


def topics_of(item: NewsItem) -> list[str]:
    text = f"{item.title} {item.summary or ''}"
    return [name for name, rx in TOPICS.items() if rx.search(text)]


def _window(ts: datetime) -> tuple[int, int, int, int, int]:
    return (ts.year, ts.month, ts.day, ts.hour, ts.minute // WINDOW_MINUTES)


def summarize(name: str, reactions: list[Reaction], horizons: tuple[int, ...] = (60, 240, 1440)) -> Pattern | None:
    windows: dict[tuple, Reaction] = {}
    for r in reactions:
        windows.setdefault(_window(r.item.event_at), r)
    if not windows:
        return None
    avg, up, samples = {}, {}, {}
    for h in horizons:
        moves = [r.changes[h] for r in windows.values() if h in r.changes]
        samples[h] = len(moves)
        if moves:
            avg[h] = sum(moves) / len(moves)
            up[h] = sum(1 for m in moves if m > 0) / len(moves)
    return Pattern(name, len(windows), avg, up, samples)


def patterns(reactions: list[Reaction], by: str = "topic") -> tuple[Pattern | None, list[Pattern]]:
    """(baseline over all news, per-topic or per-category patterns sorted by number of events)."""
    groups: dict[str, list[Reaction]] = {}
    for r in reactions:
        keys = topics_of(r.item) if by == "topic" else [r.item.category]
        for key in keys:
            groups.setdefault(key, []).append(r)
    result = [p for name, rs in groups.items() if (p := summarize(name, rs))]
    result.sort(key=lambda p: -p.events)
    return summarize("All news", reactions), result


_WORD_RE = re.compile(r"[a-z0-9$]+")
_STOP = set(
    "the a an and or of to in on for with at by from as is are was were be been after over into about amid "
    "says said say new will would could may its it this that their his her than more says live update updates "
    "what why how who when just breaking".split()
)


@lru_cache(maxsize=20000)
def _words(title: str) -> frozenset[str]:
    return frozenset(w for w in _WORD_RE.findall(title.lower()) if len(w) > 2 and w not in _STOP)


def _similarity(a: set[str], b: set[str]) -> float:
    if len(a) < 3 or not b or len(a & b) < 3:
        return 0.0
    return len(a & b) / len(a | b)


def same_story(a: NewsItem, b: NewsItem) -> bool:
    """Do two headlines describe the same story? (shared words)"""
    return _similarity(_words(a.title), _words(b.title)) >= 0.3


def related(item: NewsItem, candidates: list[NewsItem], limit: int = 5) -> list[NewsItem]:
    """Other outlets' versions of the same story (shared headline words)."""
    base = _words(item.title)
    if len(base) < 3:
        return []
    scored = []
    for c in candidates:
        if c.id == item.id or c.source == item.source:
            continue
        overlap = _similarity(base, _words(c.title))
        if overlap >= 0.3:
            scored.append((overlap, c))
    scored.sort(key=lambda x: -x[0])
    seen, out = set(), []
    for _, c in scored:
        if c.source not in seen:
            seen.add(c.source)
            out.append(c)
        if len(out) >= limit:
            break
    return out


class PatternCache:
    """Topic patterns, recomputed at most every 15 minutes (used in alerts)."""

    def __init__(self, db: Database, ttl: float = 900):
        self.db, self.ttl = db, ttl
        self._at = 0.0
        self._by_topic: dict[str, Pattern] = {}

    async def for_item(self, item: NewsItem) -> Pattern | None:
        if time.monotonic() - self._at > self.ttl:
            _, groups = patterns(await self.db.all_reactions(days=30), by="topic")
            self._by_topic = {p.name: p for p in groups}
            self._at = time.monotonic()
        found = [self._by_topic[t] for t in topics_of(item) if t in self._by_topic and 240 in self._by_topic[t].avg]
        return max(found, key=lambda p: p.events) if found else None
