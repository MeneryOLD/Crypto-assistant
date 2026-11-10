"""Application settings loaded from environment variables / .env."""

from __future__ import annotations

from functools import lru_cache
from urllib.parse import parse_qsl, urlencode, urlsplit

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

DEFAULT_NEWS_FEEDS = (
    "CoinDesk|https://www.coindesk.com/arc/outboundfeeds/rss/",
    "Cointelegraph|https://cointelegraph.com/rss",
    "Decrypt|https://decrypt.co/feed",
)


def normalize_database_url(url: str) -> str:
    """Turn a plain/Prisma-style URL into an async SQLAlchemy URL.

    ``postgresql://...?schema=public`` -> ``postgresql+asyncpg://...``
    ``sqlite:///file.db`` -> ``sqlite+aiosqlite:///file.db``
    """
    scheme, sep, rest = url.partition("://")
    if not sep:
        return url
    scheme = {"postgres": "postgresql+asyncpg", "postgresql": "postgresql+asyncpg", "sqlite": "sqlite+aiosqlite"}.get(
        scheme, scheme
    )
    # asyncpg does not understand Prisma's ?schema= parameter.
    base, qsep, query = rest.partition("?")
    if qsep:
        query = urlencode([(k, v) for k, v in parse_qsl(query) if k != "schema"])
        rest = f"{base}?{query}" if query else base
    return f"{scheme}://{rest}"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    bot_token: str = Field(..., alias="BOT_TOKEN")
    database_url: str = Field("sqlite+aiosqlite:///./crypto_intel.db", alias="DATABASE_URL")

    # Claude (AI research). Leave empty to disable /research.
    anthropic_api_key: str | None = Field(None, alias="ANTHROPIC_API_KEY")
    research_model: str = Field("claude-opus-5-5", alias="RESEARCH_MODEL")
    research_effort: str = Field("medium", alias="RESEARCH_EFFORT")
    research_cooldown_seconds: int = Field(60, alias="RESEARCH_COOLDOWN_SECONDS")

    # CoinGecko. A free "demo" key raises the rate limit but is optional.
    coingecko_api_key: str | None = Field(None, alias="COINGECKO_API_KEY")
    tracked_coins_count: int = Field(20, alias="TRACKED_COINS_COUNT")

    # "Name|url" pairs separated by commas.
    news_feeds: str = Field(",".join(DEFAULT_NEWS_FEEDS), alias="NEWS_FEEDS")

    market_poll_seconds: int = Field(300, alias="MARKET_POLL_SECONDS")
    news_poll_seconds: int = Field(600, alias="NEWS_POLL_SECONDS")

    @field_validator("database_url")
    @classmethod
    def _normalize_db(cls, value: str) -> str:
        return normalize_database_url(value)

    @property
    def feeds(self) -> list[tuple[str, str]]:
        result = []
        for entry in self.news_feeds.split(","):
            entry = entry.strip()
            if not entry:
                continue
            name, _, url = entry.partition("|")
            result.append((name.strip(), url.strip()) if url else (urlsplit(name).netloc, name.strip()))
        return result


@lru_cache
def get_settings() -> Settings:
    return Settings()  # type: ignore[call-arg]
