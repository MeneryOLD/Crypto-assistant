"""Imports the last ~90 days of headlines from the GDELT news archive (free, worldwide).

Why: the bot's "memory" (how BTC moved after each kind of news) needs many past events to be useful.
Instead of waiting months, it imports ~90 days of history once, in the background, and then records
BTC/ETH prices around every imported headline from exchange candles — exactly like live news.

GDELT allows one request every 5 seconds and is slow (10–30 s per request), so the import runs one
request at a time and saves its progress: a restart continues where it stopped.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from datetime import datetime, timedelta

import httpx

from collectors.news import Article, classify, clean_text

log = logging.getLogger(__name__)

API = "https://api.gdeltproject.org/api/v2/doc/doc"
DAYS = 90
WINDOW_DAYS = 10
MAX_RECORDS = 250


@dataclass(frozen=True, slots=True)
class Topic:
    key: str
    query: str  # GDELT query (full-text)
    title_filter: re.Pattern[str]  # the headline itself must match, so results stay on-topic
    category: str


def _rx(words: str) -> re.Pattern[str]:
    return re.compile(rf"\b({words})\b", re.I)


TOPICS: tuple[Topic, ...] = (
    Topic("bitcoin", "(bitcoin OR btc)", _rx(r"bitcoin|btc"), "crypto"),
    Topic("crypto", "(crypto OR cryptocurrency OR ethereum OR solana OR stablecoin)", _rx(r"crypto\w*|ethereum|ether|solana|xrp|stablecoins?|altcoins?"), "crypto"),
    Topic("etf", '("bitcoin etf" OR "crypto etf" OR "ether etf" OR blackrock)', _rx(r"etfs?|blackrock|ibit"), "crypto"),
    Topic("crypto-regulation", '("crypto regulation" OR "sec crypto" OR cftc OR "crypto bill" OR "stablecoin bill")', _rx(r"sec|cftc|regulat\w+|bill|law|lawsuit"), "regulation"),
    Topic("hacks", '("crypto hack" OR "exchange hack" OR exploit OR "stolen crypto")', _rx(r"hack\w*|exploit\w*|stolen|drained|breach"), "crypto"),
    Topic("fed", '("federal reserve" OR fomc OR powell OR "rate cut" OR "rate hike")', _rx(r"fed|federal reserve|fomc|powell|rates?"), "economy"),
    Topic("inflation-jobs", '(inflation OR cpi OR "jobs report" OR payrolls OR unemployment)', _rx(r"inflation|cpi|pce|jobs|payrolls|unemployment|jobless"), "economy"),
    Topic("markets", '("stock market" OR "wall street" OR "bond yields" OR recession OR "treasury yields")', _rx(r"stocks?|wall street|yields?|bonds?|recession|s&p|nasdaq|dow"), "economy"),
    Topic("oil", '("oil prices" OR opec OR crude)', _rx(r"oil|opec|crude|brent"), "economy"),
    Topic("china", '("chinese economy" OR beijing OR "xi jinping" OR yuan)', _rx(r"china|chinese|beijing|xi|yuan"), "politics"),
    Topic("trump", "trump", _rx(r"trump"), "politics"),
    Topic("trade", '(tariffs OR "trade war" OR sanctions OR "export controls")', _rx(r"tariffs?|trade war|sanctions?|export controls?"), "politics"),
    Topic("war", '(war OR missile OR invasion OR ceasefire OR airstrike)', _rx(r"war|missiles?|invasion|invades?|ceasefire|strikes?|attacks?|troops"), "politics"),
    Topic("elections", '(election OR referendum OR "election results")', _rx(r"elections?|electoral|referendum|voters?|ballots?|polls?"), "elections"),
)

# Friendly names for common domains (anything else is shown as its domain).
DOMAIN_NAMES = {
    "reuters.com": "Reuters", "apnews.com": "AP News", "bloomberg.com": "Bloomberg", "cnbc.com": "CNBC",
    "wsj.com": "WSJ", "ft.com": "Financial Times", "nytimes.com": "NYT", "washingtonpost.com": "Washington Post",
    "bbc.com": "BBC", "bbc.co.uk": "BBC", "theguardian.com": "The Guardian", "cnn.com": "CNN", "foxnews.com": "Fox News",
    "foxbusiness.com": "Fox Business", "politico.com": "Politico", "axios.com": "Axios", "aljazeera.com": "Al Jazeera",
    "forbes.com": "Forbes", "fortune.com": "Fortune", "businessinsider.com": "Business Insider", "marketwatch.com": "MarketWatch",
    "finance.yahoo.com": "Yahoo Finance", "yahoo.com": "Yahoo", "coindesk.com": "CoinDesk", "cointelegraph.com": "Cointelegraph",
    "theblock.co": "The Block", "decrypt.co": "Decrypt", "economist.com": "The Economist", "nbcnews.com": "NBC News",
    "abcnews.go.com": "ABC News", "cbsnews.com": "CBS News", "npr.org": "NPR", "scmp.com": "SCMP", "dw.com": "DW",
    "france24.com": "France 24", "independent.co.uk": "The Independent", "telegraph.co.uk": "The Telegraph",
    "thehill.com": "The Hill", "newsweek.com": "Newsweek", "time.com": "Time", "investing.com": "Investing.com",
    "fxstreet.com": "FXStreet", "barrons.com": "Barron's", "nasdaq.com": "Nasdaq", "benzinga.com": "Benzinga",
}


ANCHOR = datetime(2020, 1, 1)


def windows(now: datetime, days: int = DAYS, step: int = WINDOW_DAYS) -> list[tuple[datetime, datetime, bool]]:
    """[(start, end, complete), …] on a fixed 10-day grid covering the last `days`, newest first.

    The grid is anchored to a fixed date, so window boundaries never move: a finished window is
    imported once. The newest window is still growing; it is marked incomplete (re-read once a day).
    """
    today = now.replace(hour=0, minute=0, second=0, microsecond=0)
    oldest = today - timedelta(days=days)
    k = (today - ANCHOR).days // step
    out = []
    while True:
        start = ANCHOR + timedelta(days=k * step)
        end = min(start + timedelta(days=step), today)
        if end <= oldest:
            break
        if end > start:
            out.append((start, end, end == start + timedelta(days=step)))
        k -= 1
    return out


def plan(now: datetime) -> list[tuple[str, Topic, datetime, datetime]]:
    """Every (key, topic, start, end) request of the import, newest window first."""
    out = []
    for start, end, complete in windows(now):
        for t in TOPICS:
            key = f"history:{t.key}:{start:%Y%m%d}" + ("" if complete else f"-{end:%Y%m%d}")
            out.append((key, t, start, end))
    return out


def source_name(domain: str) -> str:
    domain = domain.lower().removeprefix("www.")
    return DOMAIN_NAMES.get(domain, domain)


def parse(topic: Topic, data: dict) -> list[Article]:
    articles = []
    for a in data.get("articles") or []:
        title = clean_text(a.get("title"))
        url = (a.get("url") or "").strip()
        if not title or not url or len(title) < 25 or not topic.title_filter.search(title):
            continue
        if (a.get("language") or "English") != "English":
            continue
        try:
            seen = datetime.strptime(a.get("seendate", ""), "%Y%m%dT%H%M%SZ")
        except ValueError:
            continue
        articles.append(
            Article(
                source=source_name(a.get("domain") or "")[:100],
                category=classify(topic.category, title, None),
                title=title[:500],
                url=url[:1000],
                summary=None,
                published_at=seen,
                via="GDELT archive",
            )
        )
    return articles


async def fetch(client: httpx.AsyncClient, topic: Topic, start: datetime, end: datetime) -> list[Article] | None:
    """One archive request. None = try again later (rate-limited or temporary error)."""
    params = {
        "query": f"{topic.query} sourcelang:english",
        "mode": "artlist",
        "format": "json",
        "maxrecords": MAX_RECORDS,
        "sort": "hybridrel",
        "startdatetime": f"{start:%Y%m%d%H%M%S}",
        "enddatetime": f"{end:%Y%m%d%H%M%S}",
    }
    try:
        resp = await client.get(API, params=params, timeout=90)
    except httpx.HTTPError as exc:
        log.info("history import: %s %s failed (%s), will retry", topic.key, f"{start:%Y-%m-%d}", type(exc).__name__)
        return None
    if resp.status_code == 429 or resp.status_code >= 500:
        return None
    if resp.status_code != 200:
        log.warning("history import: %s returned HTTP %s, skipping", topic.key, resp.status_code)
        return []
    try:
        return parse(topic, resp.json())
    except ValueError:
        # GDELT answers plain text for query errors ("Parentheses may only be used…") and when overloaded.
        text = resp.text[:120]
        if "limit requests" in text.lower() or not text.strip():
            return None  # treat as rate limit: retry later
        log.warning("history import: %s rejected by the archive: %s", topic.key, text)
        return []
