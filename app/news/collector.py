"""RSS news collection, normalisation and de-duplication."""

from __future__ import annotations

import asyncio
import hashlib
import html
import logging
import re
from calendar import timegm
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

import feedparser
import httpx
from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import NewsItem, utcnow

log = logging.getLogger(__name__)

_TAG_RE = re.compile(r"<[^>]+>")
_WS_RE = re.compile(r"\s+")
_NON_WORD_RE = re.compile(r"[^\w\s]")


@dataclass(frozen=True, slots=True)
class Article:
    source: str
    title: str
    url: str
    summary: str | None
    published_at: datetime | None


def clean_text(value: str | None, limit: int | None = None) -> str | None:
    if not value:
        return None
    # Unescape first: many feeds entity-encode their HTML (&lt;p&gt;).
    text = _WS_RE.sub(" ", html.unescape(_TAG_RE.sub(" ", html.unescape(value)))).strip()
    if limit and len(text) > limit:
        text = text[: limit - 1].rsplit(" ", 1)[0] + "…"
    return text or None


def title_fingerprint(title: str) -> str:
    """Hash of a normalised title, so the same story syndicated twice is stored once."""
    normalized = _WS_RE.sub(" ", _NON_WORD_RE.sub(" ", title.lower())).strip()
    return hashlib.sha256(normalized.encode()).hexdigest()


def parse_feed(source: str, content: bytes | str) -> list[Article]:
    parsed = feedparser.parse(content)
    articles = []
    for entry in parsed.entries:
        title = clean_text(entry.get("title"))
        url = entry.get("link")
        if not title or not url:
            continue
        struct = entry.get("published_parsed") or entry.get("updated_parsed")
        published = datetime.fromtimestamp(timegm(struct), tz=timezone.utc) if struct else None
        articles.append(
            Article(
                source=source,
                title=title[:500],
                url=url.strip()[:1000],
                summary=clean_text(entry.get("summary"), limit=1000),
                published_at=published,
            )
        )
    return articles


async def fetch_feed(client: httpx.AsyncClient, source: str, url: str) -> list[Article]:
    try:
        resp = await client.get(url, follow_redirects=True)
        resp.raise_for_status()
    except httpx.HTTPError as exc:
        log.warning("Feed %s failed: %s", source, exc)
        return []
    return parse_feed(source, resp.content)


async def store_articles(session: AsyncSession, articles: list[Article]) -> list[NewsItem]:
    """Insert articles that are new by URL and by title fingerprint. Returns the new rows."""
    unique: dict[str, tuple[str, Article]] = {}
    for a in articles:
        fp = title_fingerprint(a.title)
        if fp not in unique and all(a.url != existing.url for _, existing in unique.values()):
            unique[fp] = (fp, a)
    if not unique:
        return []

    fps = list(unique)
    urls = [a.url for _, a in unique.values()]
    existing = await session.execute(
        select(NewsItem.title_hash, NewsItem.url).where(or_(NewsItem.title_hash.in_(fps), NewsItem.url.in_(urls)))
    )
    seen_fps, seen_urls = set(), set()
    for fp, url in existing:
        seen_fps.add(fp)
        seen_urls.add(url)

    new_items = []
    for fp, a in unique.values():
        if fp in seen_fps or a.url in seen_urls:
            continue
        item = NewsItem(
            source=a.source,
            title=a.title,
            url=a.url,
            title_hash=fp,
            summary=a.summary,
            published_at=a.published_at,
            retrieved_at=utcnow(),
        )
        session.add(item)
        new_items.append(item)
    await session.commit()
    return new_items


async def collect_news(session: AsyncSession, feeds: list[tuple[str, str]]) -> list[NewsItem]:
    async with httpx.AsyncClient(timeout=20.0, headers={"user-agent": "crypto-intelligence-bot/1.0"}) as client:
        results = await asyncio.gather(*(fetch_feed(client, name, url) for name, url in feeds))
    articles = [a for batch in results for a in batch]
    return await store_articles(session, articles)


def mentions(item: NewsItem, terms: list[str]) -> bool:
    """True if the title/summary mentions any of the terms as a whole word."""
    haystack = f"{item.title} {item.summary or ''}".lower()
    for term in terms:
        term = term.lower().strip()
        if term and re.search(rf"(?<![\w$]){re.escape(term)}(?!\w)", haystack):
            return True
    return False


async def recent_news(
    session: AsyncSession,
    *,
    limit: int = 10,
    hours: int = 72,
    terms: list[str] | None = None,
) -> list[NewsItem]:
    since = utcnow() - timedelta(hours=hours)
    stmt = (
        select(NewsItem)
        .where(NewsItem.retrieved_at >= since)
        .order_by(NewsItem.published_at.desc().nulls_last(), NewsItem.id.desc())
        .limit(500 if terms else limit)
    )
    items = list((await session.scalars(stmt)).all())
    if terms:
        items = [i for i in items if mentions(i, terms)]
    return items[:limit]
