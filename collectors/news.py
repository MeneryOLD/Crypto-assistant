"""Worldwide news collection: RSS feeds, Google News topic searches and fast Telegram news channels.

Each source has its own check interval: Telegram channels (seconds-fast breaking headlines) every 20 s,
RSS feeds every minute, Google News searches every 5 minutes.
"""

from __future__ import annotations

import asyncio
import hashlib
import html
import logging
import re
import time
from calendar import timegm
from dataclasses import dataclass, field
from datetime import datetime, timezone
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
    kind: str = "rss"  # rss | google | telegram
    every: int = 60  # seconds between checks
    group: str = ""  # for the status screen


def rss(name: str, url: str, category: str, group: str) -> Source:
    return Source(name, url, category, "rss", 60, group)


def google(topic: str, query: str, category: str) -> Source:
    url = f"https://news.google.com/rss/search?q={quote_plus(query)}+when:1d&hl=en-US&gl=US&ceid=US:en"
    return Source(f"Google News: {topic}", url, category, "google", 300, "Google News (thousands of outlets)")


def telegram(name: str, channel: str, category: str) -> Source:
    return Source(name, f"https://t.me/s/{channel}", category, "telegram", 20, "⚡ Telegram breaking news")


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
    return datetime.fromtimestamp(timegm(struct), tz=timezone.utc).replace(tzinfo=None) if struct else None


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
            summary = clean_text(entry.get("summary"), limit=800)
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
        lines = [ln.strip(" -•") for ln in text.splitlines() if ln.strip(" -•")]
        title = _WS_RE.sub(" ", lines[0])
        if len(title) > 220:
            title = title[:219].rsplit(" ", 1)[0] + "…"
        rest = _WS_RE.sub(" ", " ".join(lines[1:])).strip()
        published = None
        if time_match := _TG_TIME_RE.search(body):
            try:
                published = datetime.fromisoformat(time_match.group(1)).astimezone(timezone.utc).replace(tzinfo=None)
            except ValueError:
                pass
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

    def __post_init__(self) -> None:
        self.status = {s.name: SourceStatus() for s in self.sources}

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
        headers = self._validators.get(source.url, {})
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
                st.error = (str(exc) or type(exc).__name__)[:60]
            st.ok = False
            if isinstance(exc, httpx.HTTPStatusError) and exc.response.status_code == 429:
                st.next_due = time.monotonic() + source.every * 5  # rate-limited: back off
            return []
        finally:
            st.latency_ms = int((time.monotonic() - start) * 1000)
            st.last_fetch = datetime.now(timezone.utc).replace(tzinfo=None)

        validators = {}
        if etag := resp.headers.get("etag"):
            validators["If-None-Match"] = etag
        if modified := resp.headers.get("last-modified"):
            validators["If-Modified-Since"] = modified
        self._validators[source.url] = validators

        if source.kind == "telegram":
            articles = parse_telegram(source, resp.text)
        else:
            articles = parse_feed(source, resp.content)
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
