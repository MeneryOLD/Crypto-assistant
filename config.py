"""Settings, read from environment variables or the .env file. Every setting is documented in .env.example."""

from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict

DEFAULT_X_ACCOUNTS = (
    "DeItaone,WatcherGuru,tier10k,KobeissiLetter,whale_alert,lookonchain,EmberCN,zachxbt,"
    "federalreserve,SECGov,saylor,cz_binance,elonmusk,realDonaldTrump,unusual_whales,zerohedge"
)


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    # --- required ---
    bot_token: str  # Telegram bot token from @BotFather

    # --- storage ---
    db_path: str = "data/bot.db"
    retention_days: int = 365  # news older than this is deleted (with its price history)

    # --- exchanges ---
    exchanges: str = "binance,bybit,okx,kraken"  # ccxt ids; public data only
    price_history_exchange: str = "binance"  # its 1-minute candles feed the price memory

    # --- history ---
    history_import: bool = True  # import ~90 days of past headlines from the GDELT archive on first start

    # --- optional premium sources (left empty = disabled) ---
    cryptopanic_token: str | None = None  # https://cryptopanic.com/developers/api/
    x_bearer_token: str | None = None  # X API v2 (Basic tier or higher)
    x_accounts: str = DEFAULT_X_ACCOUNTS

    # --- operations ---
    admin_ids: str = ""  # comma-separated Telegram user ids allowed to use /admin
    log_level: str = "INFO"
    log_file: str | None = None  # e.g. data/bot.log (rotated at 5 MB, 5 files kept)

    @property
    def exchange_list(self) -> list[str]:
        return [e.strip().lower() for e in self.exchanges.split(",") if e.strip()]

    @property
    def admin_list(self) -> set[int]:
        return {int(x) for x in self.admin_ids.replace(" ", "").split(",") if x.strip().lstrip("-").isdigit()}

    @property
    def x_account_list(self) -> list[str]:
        return [a.strip().lstrip("@") for a in self.x_accounts.split(",") if a.strip()]


@lru_cache
def get_settings() -> Settings:
    return Settings()  # type: ignore[call-arg]
