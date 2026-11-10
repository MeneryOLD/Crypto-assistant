"""Settings, read from environment variables or the .env file."""

from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    # Telegram bot token from @BotFather (required).
    bot_token: str

    # SQLite database file. Created automatically.
    db_path: str = "data/bot.db"

    # Exchanges to connect to (ccxt ids), comma separated. Public data only, no keys needed.
    exchanges: str = "binance,bybit,okx,kraken"

    # Exchange whose 1-minute candles are used to remember prices around each news item.
    price_history_exchange: str = "binance"

    @property
    def exchange_list(self) -> list[str]:
        return [e.strip().lower() for e in self.exchanges.split(",") if e.strip()]


@lru_cache
def get_settings() -> Settings:
    return Settings()  # type: ignore[call-arg]
