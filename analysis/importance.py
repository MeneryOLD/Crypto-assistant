"""Importance score 1–10 for every news item (rule-based; Claude can replace it later).

The score adds up simple, explainable signals:
  source quality · type of event · money involved · crypto relevance · how many outlets confirm it
and subtracts for opinion pieces, predictions and off-topic news. Each signal leaves a short reason
so users can see *why* something was rated important.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from analysis.patterns import same_story
from storage.db import NewsItem

CENTRAL_BANKS = {"Federal Reserve", "ECB", "Bank of England", "Bank of Japan", "BIS"}
REGULATORS = {"SEC", "CFTC", "ESMA (EU)", "FCA (UK)", "US Justice Dept"}  # publish many routine notices
FAST_WIRES = {"Walter Bloomberg", "FinancialJuice", "Bloomberg (TG)", "Kobeissi Letter", "Watcher Guru", "Wu Blockchain"}
MAJOR = re.compile(
    r"^(reuters|associated press|ap news|bloomberg|financial times|wall street journal|wsj|cnbc|bbc|"
    r"the guardian|new york times|nyt|washington post|politico|axios|al jazeera|coindesk|the block)",
    re.I,
)

# (points, reason, pattern) — matched against the headline
EVENTS: list[tuple[int, str, re.Pattern[str]]] = [
    (6, "rate decision", re.compile(
        r"\b((fed|federal reserve|fomc|ecb|boe|boj|pboc|rbi|snb|central bank|bank of (england|japan|canada))\b.{0,25}"
        r"\b(cuts?|raises?|hikes?|holds?|keeps?|leaves?|lowers?|lifts?)\b.{0,20}\brates?|"
        r"(cuts?|raises?|hikes?|lowers?) (interest )?rates? (by|to) \d|fomc (decision|statement))",
        re.I,
    )),
    (2, "rate outlook", re.compile(r"\b(rate (cut|hike)s?|interest rates?)\b", re.I)),
    (5, "war / attack", re.compile(r"\b(declares? war|invasion|invades|nuclear (strike|attack|test)|martial law|coup|assassinat\w+)\b", re.I)),
    (5, "collapse / default", re.compile(r"\b(insolven\w+|defaults? on|halts? withdrawals|suspends? withdrawals|depegs?|depegged|loses? (its )?peg|bank run)\b", re.I)),
    (2, "bankruptcy", re.compile(r"\b(files? for bankruptcy|declares? bankruptcy|goes bankrupt|chapter 11)\b", re.I)),
    (5, "ETF decision", re.compile(r"\b(etfs? (approv\w+|reject\w+|denied)|approves? .{0,30}etf|rejects? .{0,30}etf)\b", re.I)),
    (5, "crypto ban / reserve", re.compile(r"\b(bans? (bitcoin|crypto\w*)|strategic bitcoin reserve|bitcoin reserve|legal tender)\b", re.I)),
    (3, "hack / exploit", re.compile(r"\b(hack(ed|s)?|exploit(ed)?|drained|stolen|breach)\b", re.I)),
    (3, "key economic data", re.compile(r"\b(cpi|pce|nonfarm|payrolls|jobs report|unemployment rate|gdp)\b", re.I)),
    (3, "regulator action", re.compile(r"\b(sec|cftc|doj|justice department) (sues|charges|approves|rejects|settles|drops)\b", re.I)),
    (1, "legal case", re.compile(r"\b(charged|indicted|sentenced|arrested|pleads? guilty)\b", re.I)),
    (2, "central banker", re.compile(r"\b(powell|lagarde|bailey|ueda|fed chair|fed('s)? \w+ says|fomc)\b", re.I)),
    (2, "sanctions / tariffs", re.compile(r"\b(sanctions?|tariffs?|trade war|export (ban|controls?))\b", re.I)),
    (2, "military escalation", re.compile(r"\b(missiles?|air ?strikes?|strikes? on|troops|ceasefire|drone attack|shelling)\b", re.I)),
    (2, "big market move", re.compile(r"\b(all[- ]time high|record high|crash(es|ed)?|collapses?|plunges?|soars?|surges?|tumbles?|liquidat\w+|sell-?off|rall(y|ies))\b", re.I)),
    (2, "law / executive order", re.compile(r"\b(executive order|signs? (bill|law)|passes? (bill|law)|stablecoin (bill|law)|market structure bill|clarity act|genius act)\b", re.I)),
    (2, "election outcome", re.compile(r"\b(wins? (the )?(election|presidency|vote)|election results?|concedes|resigns?|steps down|snap election)\b", re.I)),
    (2, "institutional flows", re.compile(r"\b(etf (inflows?|outflows?)|blackrock|strategy (buys|acquires)|saylor|treasury (buys|adds))\b", re.I)),
    (1, "whales / on-chain", re.compile(r"\b(whales?|on-?chain|exchange (reserves|outflows|inflows))\b", re.I)),
]
DECISIVE = {"rate decision", "war / attack", "collapse / default", "ETF decision", "crypto ban / reserve", "regulator action"}
BREAKING = re.compile(r"^\W*(breaking|just in|urgent|alert|flash)\b|🚨", re.I)
CRYPTO = re.compile(r"\b(bitcoin|btc|ethereum|eth|crypto\w*|stablecoins?|blockchain|solana|xrp|binance|coinbase|tether|defi)\b", re.I)
BILLIONS = re.compile(r"\$\s?\d[\d.,]*\s?(billion|bn|trillion|tn)\b|\$\d[\d.,]*[bt]\b", re.I)
MILLIONS = re.compile(r"\$\s?(\d[\d.,]*)\s?(million|m)\b", re.I)
RAW_DOLLARS = re.compile(r"\$\s?(\d{1,3}(?:,\d{3}){2,})(?![\d,])")  # "$200,000,000"
SOFT = re.compile(
    r"(^live\b|price prediction|predicts?|could (reach|hit)|opinion|analysis:|podcast|review|how to|what to know|explained|"
    r"here'?s (why|what|how)|\| letters|live updates?|live:|guide|sponsored|webinar|quiz|recap|newsletter|"
    r"\bweekly\b|\bdaily (crypto )?discussion|top \d+|best \w+ to buy|\?$)",
    re.I,
)
# Talk about a possible event, not the event itself ("rate hike odds rise", "X warns of…").
SPECULATION = re.compile(
    r"\b(expect\w*|projected|odds|could|may|might|calls? for|backs?|floats?|warns?|bets?|predict\w*|"
    r"likely|considers?|mulls?|weighs?|eyes|case for|chance|fears?|premature|plans to|supports?)\b",
    re.I,
)
OFF_TOPIC = re.compile(r"\b(football|soccer|nba|nfl|tennis|celebrity|movie|album|recipe|fashion|royal family|horoscope)\b", re.I)

LEVELS = [(9, "🔴", "Critical"), (7, "🟠", "Important"), (5, "🟡", "Notable"), (1, "⚪", "Minor")]


@dataclass(frozen=True, slots=True)
class Score:
    value: int
    reasons: list[str]


def _max_millions(text: str) -> float:
    best = max((int(m.group(1).replace(",", "")) / 1e6 for m in RAW_DOLLARS.finditer(text)), default=0.0)
    for m in MILLIONS.finditer(text):
        try:
            best = max(best, float(m.group(1).replace(",", "")))
        except ValueError:
            pass
    return best


def level(score: int | None) -> tuple[str, str]:
    """(emoji, label) for a score."""
    for threshold, emoji, label in LEVELS:
        if (score or 0) >= threshold:
            return emoji, label
    return "⚪", "Minor"


def score(item: NewsItem, nearby: list[NewsItem] | None = None) -> Score:
    """Score one item. `nearby` = other items from the last hours (for cross-outlet confirmation)."""
    title = item.title
    text = f"{title} {item.summary or ''}"
    points = 2.0
    reasons: list[str] = []

    if item.source in CENTRAL_BANKS:
        points += 2
        reasons.append("central bank")
    elif item.source in REGULATORS:
        points += 1
        reasons.append("official regulator")
    elif item.source in FAST_WIRES:
        points += 1
        reasons.append("breaking wire")
    elif MAJOR.match(item.source):
        points += 1
        reasons.append("major outlet")

    event_points = 0
    decisive = False
    for pts, reason, rx in EVENTS:
        if reason == "rate outlook" and "rate decision" in reasons:
            continue
        if rx.search(title):
            if reason == "hack / exploit" and not CRYPTO.search(text):
                pts = 1  # a generic theft/breach story, not a crypto hack
            if event_points < 6:  # cap so headlines stuffed with keywords don't max out
                reasons.append(reason)
            event_points += pts
            decisive = decisive or reason in DECISIVE
    points += min(event_points, 6)

    if BREAKING.search(title):
        points += 1
        reasons.append("flagged breaking")
    if BILLIONS.search(text) or _max_millions(text) >= 1000:
        points += 2
        reasons.append("$ billions")
    elif _max_millions(text) >= 100:
        points += 1
        reasons.append("$100M+")
    if CRYPTO.search(text):
        points += 1
        reasons.append("crypto-related")

    if nearby:
        outlets = {n.source for n in nearby if n.id != item.id and n.source != item.source and same_story(item, n)}
        if len(outlets) >= 4:
            points += 2
            reasons.append(f"{len(outlets) + 1} outlets")
        elif len(outlets) >= 2:
            points += 1
            reasons.append(f"{len(outlets) + 1} outlets")

    if SOFT.search(title):
        points -= 2
        reasons.append("opinion / analysis")
    elif event_points >= 2 and SPECULATION.search(title):
        points -= 2
        reasons.append("outlook, not a decision")
    if OFF_TOPIC.search(title):
        points -= 3
        reasons.append("off-topic")

    return Score(max(1, min(10, round(points))), reasons)
