"""Worldwide news collection: RSS feeds, Google News topic searches and fast Telegram news channels.

Each source has its own check interval: Telegram channels (seconds-fast breaking headlines) every 20 s,
RSS feeds every minute, Google News searches every 5 minutes.
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import html
import logging
import re
import time
from calendar import timegm
from dataclasses import dataclass, field
from datetime import UTC, datetime
from urllib.parse import quote_plus

import feedparser
import httpx

log = logging.getLogger(__name__)

CATEGORIES = ("crypto", "politics", "elections", "economy", "regulation")


@dataclass(frozen=True, slots=True)
class Source:
    name: str
    url: str
    category: str  # default category for items from this source
    kind: str = "rss"  # rss | google | telegram | reddit | cryptopanic | x
    every: int = 60  # seconds between checks
    group: str = ""  # for the status screen
    min_usd: float = 0  # Telegram only: skip posts mentioning less money (Whale Alert)


def rss(name: str, url: str, category: str, group: str) -> Source:
    return Source(name, url, category, "rss", 60, group)


def google(topic: str, query: str, category: str) -> Source:
    url = f"https://news.google.com/rss/search?q={quote_plus(query)}+when:1d&hl=en-US&gl=US&ceid=US:en"
    return Source(f"Google News: {topic}", url, category, "google", 300, "Google News (thousands of outlets)")


def telegram(name: str, channel: str, category: str, group: str = "⚡ Telegram breaking news", min_usd: float = 0) -> Source:
    return Source(name, f"https://t.me/s/{channel}", category, "telegram", 20, group, min_usd)


def reddit(subreddit: str, category: str) -> Source:
    # Reddit rate-limits anonymous readers hard: one feed every 10 minutes, start times spread out.
    return Source(f"r/{subreddit}", f"https://www.reddit.com/r/{subreddit}/.rss", category, "reddit", 600, "Reddit (community)")


SOURCES: tuple[Source, ...] = (
    # ⚡ Fast: public Telegram channels that post headlines seconds after events
    telegram("Walter Bloomberg", "WalterBloomberg", "economy"),
    telegram("FinancialJuice", "financialjuice", "economy"),
    telegram("Bloomberg (TG)", "bloomberg", "economy"),
    telegram("Kobeissi Letter", "thekobeissiletter", "economy"),
    telegram("Watcher Guru", "WatcherGuru", "crypto"),
    telegram("Wu Blockchain", "wublockchainenglish", "crypto"),
    telegram("CryptoQuant", "cryptoquant_official", "crypto"),
    telegram("BRICS News", "BRICSNews", "politics"),
    telegram("Disclose.tv", "disclosetv", "politics"),
    telegram("Insider Paper", "insiderpaper", "politics"),
    telegram("World News (TG)", "WorldNews", "politics"),
    telegram("Al Jazeera (TG)", "AJENews_Official", "politics"),
    telegram("CoinDesk (TG)", "CoinDeskGlobal", "crypto"),
    telegram("The Block (TG)", "the_block_crypto", "crypto"),
    # Exchange announcements: listings and delistings move prices within minutes
    telegram("Binance Announcements", "binance_announcements", "crypto", "🏦 Exchange announcements"),
    telegram("Bybit Announcements", "Bybit_Announcements", "crypto", "🏦 Exchange announcements"),
    telegram("OKX Announcements", "OKXAnnouncements", "crypto", "🏦 Exchange announcements"),
    # On-chain: whale transfers (only $50M+), analytics
    telegram("Whale Alert", "whale_alert_io", "crypto", "🐋 On-chain", min_usd=50_000_000),
    telegram("Glassnode", "glassnode", "crypto", "🐋 On-chain"),
    telegram("Unfolded", "unfolded", "crypto", "🐋 On-chain"),
    # Crypto media
    rss("CoinDesk", "https://www.coindesk.com/arc/outboundfeeds/rss/", "crypto", "Crypto media"),
    rss("Cointelegraph", "https://cointelegraph.com/rss", "crypto", "Crypto media"),
    rss("Decrypt", "https://decrypt.co/feed", "crypto", "Crypto media"),
    rss("The Block", "https://www.theblock.co/rss.xml", "crypto", "Crypto media"),
    rss("Bitcoin Magazine", "https://bitcoinmagazine.com/.rss/full/", "crypto", "Crypto media"),
    rss("Blockworks", "https://blockworks.co/feed", "crypto", "Crypto media"),
    rss("DL News", "https://www.dlnews.com/arc/outboundfeeds/rss/", "crypto", "Crypto media"),
    rss("The Defiant", "https://thedefiant.io/api/feed", "crypto", "Crypto media"),
    rss("CryptoSlate", "https://cryptoslate.com/feed/", "crypto", "Crypto media"),
    rss("Protos", "https://protos.com/feed/", "crypto", "Crypto media"),
    rss("Unchained", "https://unchainedcrypto.com/feed/", "crypto", "Crypto media"),
    rss("Crypto Briefing", "https://cryptobriefing.com/feed/", "crypto", "Crypto media"),
    rss("Bitcoin.com News", "https://news.bitcoin.com/feed/", "crypto", "Crypto media"),
    rss("CryptoPotato", "https://cryptopotato.com/feed/", "crypto", "Crypto media"),
    rss("BeInCrypto", "https://beincrypto.com/feed/", "crypto", "Crypto media"),
    rss("CoinGape", "https://coingape.com/feed/", "crypto", "Crypto media"),
    rss("U.Today", "https://u.today/rss", "crypto", "Crypto media"),
    rss("NewsBTC", "https://www.newsbtc.com/feed/", "crypto", "Crypto media"),
    rss("Bitcoinist", "https://bitcoinist.com/feed/", "crypto", "Crypto media"),
    rss("CryptoNews", "https://cryptonews.com/news/feed/", "crypto", "Crypto media"),
    # World news
    rss("BBC World", "https://feeds.bbci.co.uk/news/world/rss.xml", "politics", "World news"),
    rss("Al Jazeera", "https://www.aljazeera.com/xml/rss/all.xml", "politics", "World news"),
    rss("Guardian World", "https://www.theguardian.com/world/rss", "politics", "World news"),
    rss("NYT World", "https://rss.nytimes.com/services/xml/rss/nyt/World.xml", "politics", "World news"),
    rss("DW", "https://rss.dw.com/rdf/rss-en-world", "politics", "World news"),
    rss("France 24", "https://www.france24.com/en/rss", "politics", "World news"),
    rss("Sky News World", "https://feeds.skynews.com/feeds/rss/world.xml", "politics", "World news"),
    rss("CNN World", "http://rss.cnn.com/rss/edition_world.rss", "politics", "World news"),
    rss("CBS World", "https://www.cbsnews.com/latest/rss/world", "politics", "World news"),
    rss("ABC News Intl", "https://abcnews.go.com/abcnews/internationalheadlines", "politics", "World news"),
    rss("NPR World", "https://feeds.npr.org/1004/rss.xml", "politics", "World news"),
    rss("Le Monde (EN)", "https://www.lemonde.fr/en/rss/une.xml", "politics", "World news"),
    rss("Time", "https://time.com/feed/", "politics", "World news"),
    # Regions
    rss("South China Morning Post", "https://www.scmp.com/rss/91/feed", "politics", "Regions"),
    rss("Japan Times", "https://www.japantimes.co.jp/feed/", "politics", "Regions"),
    rss("Times of India", "https://timesofindia.indiatimes.com/rssfeeds/296589292.cms", "politics", "Regions"),
    rss("Moscow Times", "https://www.themoscowtimes.com/rss/news", "politics", "Regions"),
    rss("Middle East Eye", "https://www.middleeasteye.net/rss", "politics", "Regions"),
    rss("Africanews", "https://www.africanews.com/feed/rss", "politics", "Regions"),
    rss("Latin Times", "https://www.latintimes.com/rss", "politics", "Regions"),
    # US politics
    rss("Guardian US Politics", "https://www.theguardian.com/us-news/us-politics/rss", "politics", "US politics"),
    rss("NYT Politics", "https://rss.nytimes.com/services/xml/rss/nyt/Politics.xml", "politics", "US politics"),
    rss("Politico", "https://rss.politico.com/politics-news.xml", "politics", "US politics"),
    rss("NPR Politics", "https://feeds.npr.org/1014/rss.xml", "politics", "US politics"),
    rss("The Hill", "https://thehill.com/news/feed/", "politics", "US politics"),
    rss("Axios", "https://api.axios.com/feed/", "politics", "US politics"),
    rss("Fox News Politics", "https://moxie.foxnews.com/google-publisher/politics.xml", "politics", "US politics"),
    rss("Washington Post Politics", "https://feeds.washingtonpost.com/rss/politics", "politics", "US politics"),
    rss("Bloomberg Politics", "https://feeds.bloomberg.com/politics/news.rss", "politics", "US politics"),
    # Economy & markets
    rss("Bloomberg Markets", "https://feeds.bloomberg.com/markets/news.rss", "economy", "Economy & markets"),
    rss("WSJ Markets", "https://feeds.a.dj.com/rss/RSSMarketsMain.xml", "economy", "Economy & markets"),
    rss("CNBC Economy", "https://www.cnbc.com/id/20910258/device/rss/rss.html", "economy", "Economy & markets"),
    rss("CNBC Top News", "https://www.cnbc.com/id/100003114/device/rss/rss.html", "economy", "Economy & markets"),
    rss("CNBC Finance", "https://www.cnbc.com/id/10000664/device/rss/rss.html", "economy", "Economy & markets"),
    rss("MarketWatch", "https://feeds.content.dowjones.io/public/rss/mw_topstories", "economy", "Economy & markets"),
    rss("BBC Business", "https://feeds.bbci.co.uk/news/business/rss.xml", "economy", "Economy & markets"),
    rss("ForexLive", "https://www.forexlive.com/feed/news", "economy", "Economy & markets"),
    rss("FXStreet", "https://www.fxstreet.com/rss/news", "economy", "Economy & markets"),
    rss("Investing.com", "https://www.investing.com/rss/news.rss", "economy", "Economy & markets"),
    rss("The Economist", "https://www.economist.com/finance-and-economics/rss.xml", "economy", "Economy & markets"),
    rss("ZeroHedge", "https://feeds.feedburner.com/zerohedge/feed", "economy", "Economy & markets"),
    # Central banks & official
    rss("Federal Reserve", "https://www.federalreserve.gov/feeds/press_all.xml", "economy", "Central banks"),
    rss("ECB", "https://www.ecb.europa.eu/rss/press.html", "economy", "Central banks"),
    rss("Bank of England", "https://www.bankofengland.co.uk/rss/news", "economy", "Central banks"),
    rss("Bank of Japan", "https://www.boj.or.jp/en/rss/whatsnew.xml", "economy", "Central banks"),
    rss("BIS", "https://www.bis.org/doclist/all_pressrels.rss", "economy", "Central banks"),
    # Regulators & law enforcement
    rss("SEC", "https://www.sec.gov/news/pressreleases.rss", "regulation", "Regulators"),
    rss("CFTC", "https://www.cftc.gov/RSS/RSSGP/rssgp.xml", "regulation", "Regulators"),
    rss("ESMA (EU)", "https://www.esma.europa.eu/rss.xml", "regulation", "Regulators"),
    rss("FCA (UK)", "https://www.fca.org.uk/news/rss.xml", "regulation", "Regulators"),
    rss("US Justice Dept", "https://www.justice.gov/news/rss?type=press_release", "regulation", "Regulators"),
    # Google News topic searches: headlines from thousands of outlets (Reuters, AP, Bloomberg, …)
    google("Bitcoin", "bitcoin OR BTC", "crypto"),
    google("Crypto regulation", "crypto regulation OR SEC crypto OR stablecoin law", "regulation"),
    google("Federal Reserve", '"Federal Reserve" OR Powell OR "interest rates"', "economy"),
    google("Inflation & jobs", "CPI inflation OR jobs report OR nonfarm payrolls", "economy"),
    google("Tariffs & trade", "tariffs OR trade war OR sanctions", "politics"),
    google("Elections", "election results OR presidential election OR snap election", "elections"),
    google("Wars & conflicts", "war OR missile strike OR ceasefire OR invasion", "politics"),
    google("Ethereum & altcoins", "ethereum OR solana OR XRP OR altcoins", "crypto"),
    google("Crypto ETFs", "bitcoin ETF OR crypto ETF OR ether ETF", "crypto"),
    google("Crypto hacks", "crypto hack OR exchange hack OR DeFi exploit", "crypto"),
    google("Trump & crypto", "Trump crypto OR Trump bitcoin OR White House crypto", "politics"),
    google("China economy", "China economy OR PBOC OR yuan", "economy"),
    google("Oil & OPEC", "oil prices OR OPEC OR crude oil", "economy"),
    google("Yields & banks", "Treasury yields OR bond market OR bank failure", "economy"),
    google("Middle East", "Iran OR Israel OR Gaza OR Saudi", "politics"),
    google("Russia & Ukraine", "Russia Ukraine OR Kremlin OR Zelensky", "politics"),
    # Community (Reddit): noisy, so scored lower; useful for retail mood
    reddit("CryptoCurrency", "crypto"),
    reddit("Bitcoin", "crypto"),
    reddit("ethereum", "crypto"),
    reddit("CryptoMarkets", "crypto"),
    reddit("worldnews", "politics"),
    reddit("Economics", "economy"),
    reddit("geopolitics", "politics"),
)

# Google News also returns small local sites; keep only well-known outlets.
MAJOR_OUTLETS = (
    "reuters", "associated press", "ap news", "bloomberg", "financial times", "wall street journal", "wsj",
    "new york times", "washington post", "bbc", "the guardian", "cnbc", "cnn", "nbc news", "abc news", "cbs news",
    "fox news", "fox business", "politico", "axios", "the hill", "al jazeera", "afp", "france 24", "dw",
    "the economist", "forbes", "fortune", "business insider", "yahoo finance", "marketwatch", "barron's",
    "nikkei", "south china morning post", "the japan times", "times of india", "newsweek", "time", "npr",
    "sky news", "euronews", "le monde", "the independent", "the telegraph", "kyiv independent", "haaretz",
    "times of israel", "coindesk", "cointelegraph", "the block", "decrypt", "bitcoin magazine", "blockworks",
    "dl news", "fxstreet", "investing.com", "seeking alpha", "federal reserve", "white house", "sec.gov",
)


def is_major_outlet(publisher: str) -> bool:
    p = publisher.lower()
    return any(p == o or p.startswith(o + " ") or p.startswith("the " + o) for o in MAJOR_OUTLETS)


_ELECTION_RE = re.compile(
    r"\b(elections?|electoral|voters?|ballots?|polls? (?:show|open|close)|polling stations?|referendum|"
    r"primar(?:y|ies)|run-?off|candidates?|inaugurat\w*|exit polls?|swing states?|midterms?|re-?elect\w*)\b",
    re.I,
)
_CRYPTO_RE = re.compile(
    r"\b(bitcoin|btc|ethereum|ether|crypto\w*|stablecoins?|blockchain|tokens?|defi|solana|xrp|"
    r"binance|coinbase|tether|usdt|usdc|cbdc|digital assets?|spot etf)\b",
    re.I,
)
_REGULATION_RE = re.compile(r"\b(sec|cftc|regulat\w*|lawsuit|enforcement|ban(?:s|ned)?|license[ds]?|sanctions?)\b", re.I)

_TAG_RE = re.compile(r"<[^>]+>")
_BR_RE = re.compile(r"<br\s*/?>", re.I)
_WS_RE = re.compile(r"\s+")
_NON_WORD_RE = re.compile(r"[^\w\s]")

# t.me/s/<channel> page parsing
_TG_MESSAGE_RE = re.compile(r'class="tgme_widget_message [^"]*"[^>]*data-post="([^"]+)"(.*?)(?=class="tgme_widget_message |\Z)', re.S)
_TG_TEXT_RE = re.compile(r'<div class="tgme_widget_message_text[^"]*"[^>]*>(.*?)</div>', re.S)
_TG_TIME_RE = re.compile(r'<time datetime="([^"]+)"')
_TG_SELF_MENTION_RE = re.compile(r"\(?@\w+\)?\s*$")
_TG_SIGNATURE_RE = re.compile(r"\s*\|\s*FJ(?![A-Za-z])", re.I)  # FinancialJuice appends "|FJ"


@dataclass(frozen=True, slots=True)
class Article:
    source: str
    category: str
    title: str
    url: str
    summary: str | None
    published_at: datetime | None  # naive UTC
    via: str = ""  # name of the Source it came through (differs from `source` for Google News)


@dataclass(slots=True)
class SourceStatus:
    ok: bool | None = None  # None = not fetched yet
    items: int = 0
    latency_ms: int = 0
    error: str | None = None
    last_fetch: datetime | None = None
    new_total: int = 0
    next_due: float = 0.0  # monotonic time of the next check


def clean_text(value: str | None, limit: int | None = None) -> str | None:
    if not value:
        return None
    # Unescape first: many feeds entity-encode their HTML (&lt;p&gt;).
    text = _WS_RE.sub(" ", html.unescape(_TAG_RE.sub(" ", html.unescape(value)))).strip()
    if limit and len(text) > limit:
        text = text[: limit - 1].rsplit(" ", 1)[0] + "…"
    return text or None


def title_fingerprint(title: str) -> str:
    """Hash of the normalised title, so the same headline on two sites is stored once."""
    normalized = _WS_RE.sub(" ", _NON_WORD_RE.sub(" ", title.lower())).strip()
    return hashlib.sha256(normalized.encode()).hexdigest()


def classify(default: str, title: str, summary: str | None) -> str:
    text = f"{title} {summary or ''}"
    if default != "crypto" and _CRYPTO_RE.search(text):
        return "regulation" if default == "regulation" else "crypto"
    if default in ("politics", "elections") and _ELECTION_RE.search(title):  # title only: summaries mention votes too often
        return "elections"
    if default == "elections":
        return "politics"
    if default == "regulation" or (default == "crypto" and _REGULATION_RE.search(title)):
        return "regulation"
    return default


def _published(entry: dict) -> datetime | None:
    struct = entry.get("published_parsed") or entry.get("updated_parsed")
    return datetime.fromtimestamp(timegm(struct), tz=UTC).replace(tzinfo=None) if struct else None


def parse_feed(source: Source, content: bytes | str) -> list[Article]:
    """RSS/Atom feed (also Google News, whose titles end with ' - Publisher')."""
    articles = []
    for entry in feedparser.parse(content).entries:
        title = clean_text(entry.get("title"))
        url = (entry.get("link") or "").strip()
        if not title or not url:
            continue
        name = source.name
        if source.kind == "google":
            publisher = (entry.get("source") or {}).get("title")
            if not publisher or not is_major_outlet(publisher):
                continue
            if title.endswith(f" - {publisher}"):
                title = title[: -len(publisher) - 3].strip()
            name = publisher or name
            summary = None  # Google's summary is just the title again
        else:
            summary = clean_text(entry.get("summary") or entry.get("content", [{}])[0].get("value"), limit=800)
            if source.kind == "reddit" and summary:
                summary = _REDDIT_FOOTER_RE.sub("", summary).strip() or None
        articles.append(
            Article(
                source=name[:100],
                category=classify(source.category, title, summary),
                title=title[:500],
                url=url[:1000],
                summary=summary,
                published_at=_published(entry),
                via=source.name,
            )
        )
    return articles


_REDDIT_FOOTER_RE = re.compile(r"\s*submitted by\s+/u/\S+.*$", re.I | re.S)
_USD_PAREN_RE = re.compile(r"\(\s*([\d,]+(?:\.\d+)?)\s*USD\s*\)", re.I)
_USD_WORDS_RE = re.compile(r"\$\s?(\d[\d,.]*)\s*(billion|bn|million|m)\b", re.I)


def usd_amount(text: str) -> float:
    """Largest USD amount mentioned: "(73,500,000 USD)" or "$1.2 billion"."""
    amounts = [float(m.group(1).replace(",", "")) for m in _USD_PAREN_RE.finditer(text)]
    for m in _USD_WORDS_RE.finditer(text):
        try:
            value = float(m.group(1).replace(",", ""))
        except ValueError:
            continue
        amounts.append(value * (1e9 if m.group(2).lower() in ("billion", "bn") else 1e6))
    return max(amounts, default=0.0)


def parse_cryptopanic(source: Source, data: dict) -> list[Article]:
    """CryptoPanic API (developer v2 / v1 shapes). Community votes go into the summary."""
    articles = []
    for p in data.get("results") or []:
        title = clean_text(p.get("title"))
        if not title:
            continue
        url = p.get("original_url") or p.get("url") or f"https://cryptopanic.com/news/{p.get('id')}/{p.get('slug', '')}"
        votes = p.get("votes") or {}
        vote_text = None
        if votes:
            vote_text = (
                f"CryptoPanic votes: 👍 {votes.get('positive', 0)} · 👎 {votes.get('negative', 0)} · "
                f"❗ {votes.get('important', 0)}"
            )
        try:
            published = datetime.fromisoformat((p.get("published_at") or "").replace("Z", "+00:00")).astimezone(UTC).replace(tzinfo=None)
        except ValueError:
            published = None
        publisher = (p.get("source") or {}).get("title") or source.name
        summary = clean_text(p.get("description"), limit=600)
        articles.append(
            Article(
                source=publisher[:100],
                category=classify(source.category, title, summary),
                title=title[:500],
                url=url[:1000],
                summary=" · ".join(x for x in (summary, vote_text) if x) or None,
                published_at=published,
                via=source.name,
            )
        )
    return articles


def parse_x(source: Source, data: dict) -> list[Article]:
    """X (Twitter) API v2 recent-search response."""
    users = {u["id"]: u.get("username", "") for u in (data.get("includes") or {}).get("users", [])}
    articles = []
    for t in data.get("data") or []:
        text = html.unescape(t.get("text") or "").strip()
        if len(text) < 15 or text.startswith("RT @"):
            continue
        username = users.get(t.get("author_id"), "unknown")
        first, _, rest = text.partition("\n")
        title = _WS_RE.sub(" ", first)[:220]
        try:
            published = datetime.fromisoformat((t.get("created_at") or "").replace("Z", "+00:00")).astimezone(UTC).replace(tzinfo=None)
        except ValueError:
            published = None
        articles.append(
            Article(
                source=f"X: @{username}"[:100],
                category=classify(source.category, title, rest),
                title=title,
                url=f"https://x.com/{username}/status/{t.get('id')}",
                summary=_WS_RE.sub(" ", rest).strip()[:800] or None,
                published_at=published,
                via=source.name,
            )
        )
    return articles


def premium_sources(
    cryptopanic_token: str | None = None, x_bearer_token: str | None = None, x_accounts: list[str] | None = None
) -> tuple[Source, ...]:
    """Optional paid/keyed sources — only added when their key is configured."""
    out = []
    if cryptopanic_token:
        out.append(
            Source(
                "CryptoPanic",
                f"https://cryptopanic.com/api/developer/v2/posts/?auth_token={cryptopanic_token}&public=true&kind=news",
                "crypto", "cryptopanic", 120, "🔑 Premium APIs",
            )
        )
    if x_bearer_token and x_accounts:
        query = "(" + " OR ".join(f"from:{a.lstrip('@')}" for a in x_accounts) + ") -is:retweet -is:reply"
        url = (
            "https://api.x.com/2/tweets/search/recent?max_results=50&tweet.fields=created_at,author_id"
            f"&expansions=author_id&user.fields=username&query={quote_plus(query)}"
        )
        out.append(Source("X (Twitter)", url, "economy", "x", 60, "🔑 Premium APIs"))
    return tuple(out)


def parse_telegram(source: Source, page: str) -> list[Article]:
    """Messages from a public channel page (t.me/s/<channel>)."""
    articles = []
    for post, body in _TG_MESSAGE_RE.findall(page):
        text_match = _TG_TEXT_RE.search(body)
        if not text_match:
            continue  # photo/video without text
        text = html.unescape(_TAG_RE.sub("", _BR_RE.sub("\n", text_match.group(1))))
        text = _TG_SELF_MENTION_RE.sub("", text).strip()
        text = _TG_SIGNATURE_RE.sub("", text).strip()
        if len(text) < 15:
            continue
        if source.min_usd and usd_amount(text) < source.min_usd:
            continue  # e.g. Whale Alert: only transfers worth at least $50M
        lines = [ln.strip(" -•") for ln in text.splitlines() if ln.strip(" -•")]
        title = _WS_RE.sub(" ", lines[0])
        if len(title) > 220:
            title = title[:219].rsplit(" ", 1)[0] + "…"
        rest = _WS_RE.sub(" ", " ".join(lines[1:])).strip()
        published = None
        if time_match := _TG_TIME_RE.search(body):
            with contextlib.suppress(ValueError):
                published = datetime.fromisoformat(time_match.group(1)).astimezone(UTC).replace(tzinfo=None)
        articles.append(
            Article(
                source=source.name,
                category=classify(source.category, title, rest),
                title=title,
                url=f"https://t.me/{post}",
                summary=rest[:800] or None,
                published_at=published,
                via=source.name,
            )
        )
    return articles


@dataclass
class NewsCollector:
    sources: tuple[Source, ...] = SOURCES
    status: dict[str, SourceStatus] = field(default_factory=dict)
    _validators: dict[str, dict[str, str]] = field(default_factory=dict)  # ETag / Last-Modified per feed
    _http: httpx.AsyncClient | None = None
    _limit: asyncio.Semaphore = field(default_factory=lambda: asyncio.Semaphore(20))
    x_bearer_token: str | None = None

    def __post_init__(self) -> None:
        self.status = {s.name: SourceStatus() for s in self.sources}
        # Spread Reddit feeds over time: Reddit blocks bursts from one address.
        now = time.monotonic()
        for i, s in enumerate(x for x in self.sources if x.kind == "reddit"):
            self.status[s.name].next_due = now + 30 + i * 60

    @property
    def http(self) -> httpx.AsyncClient:
        if self._http is None:
            self._http = httpx.AsyncClient(
                timeout=15.0,
                follow_redirects=True,
                headers={
                    "user-agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                    "(KHTML, like Gecko) Chrome/131.0 Safari/537.36"
                },
            )
        return self._http

    async def close(self) -> None:
        if self._http is not None:
            await self._http.aclose()

    async def _fetch(self, source: Source) -> list[Article]:
        st = self.status[source.name]
        st.next_due = time.monotonic() + source.every
        start = time.monotonic()
        headers = dict(self._validators.get(source.url, {}))
        if source.kind == "x" and self.x_bearer_token:
            headers["Authorization"] = f"Bearer {self.x_bearer_token}"
        try:
            async with self._limit:
                resp = await self.http.get(source.url, headers=headers)
            if resp.status_code == 304:  # not modified since last check — nothing new, very cheap
                st.ok, st.error = True, None
                return []
            resp.raise_for_status()
        except httpx.HTTPError as exc:
            if isinstance(exc, httpx.HTTPStatusError):
                st.error = f"HTTP {exc.response.status_code}"
            elif isinstance(exc, httpx.TimeoutException):
                st.error = "timed out"
            else:
                st.error = type(exc).__name__  # never str(exc): it can contain the URL with an API key
            st.ok = False
            if isinstance(exc, httpx.HTTPStatusError) and exc.response.status_code == 429:
                st.next_due = time.monotonic() + source.every * 5  # rate-limited: back off
            return []
        finally:
            st.latency_ms = int((time.monotonic() - start) * 1000)
            st.last_fetch = datetime.now(UTC).replace(tzinfo=None)

        validators = {}
        if etag := resp.headers.get("etag"):
            validators["If-None-Match"] = etag
        if modified := resp.headers.get("last-modified"):
            validators["If-Modified-Since"] = modified
        self._validators[source.url] = validators

        try:
            if source.kind == "telegram":
                articles = parse_telegram(source, resp.text)
            elif source.kind == "cryptopanic":
                articles = parse_cryptopanic(source, resp.json())
            elif source.kind == "x":
                articles = parse_x(source, resp.json())
            else:
                articles = parse_feed(source, resp.content)
        except (ValueError, KeyError, TypeError) as exc:  # a source changed its format
            st.ok, st.error = False, f"unreadable response ({type(exc).__name__})"
            return []
        st.ok, st.error, st.items = True, None, len(articles)
        return articles

    def due(self, kinds: set[str] | None = None) -> list[Source]:
        now = time.monotonic()
        return [
            s for s in self.sources if self.status[s.name].next_due <= now and (kinds is None or s.kind in kinds)
        ]

    async def poll(self, *, only_due: bool = False, kinds: set[str] | None = None) -> list[Article]:
        """Fetch sources at once (all, or only those whose interval has passed), optionally of some kinds."""
        sources = self.due(kinds) if only_due else [s for s in self.sources if kinds is None or s.kind in kinds]
        results = await asyncio.gather(*(self._fetch(s) for s in sources))
        return [a for batch in results for a in batch]

    def source_of(self, name: str) -> Source | None:
        return next((s for s in self.sources if s.name == name), None)
