"""Small formatting helpers shared by all screens."""

from __future__ import annotations

from datetime import datetime, timezone
from html import escape

CATEGORY_ICONS = {"crypto": "🪙", "politics": "🏛", "elections": "🗳", "economy": "📊", "regulation": "⚖️"}
EXCHANGE_NAMES = {"okx": "OKX", "bybit": "Bybit", "binance": "Binance", "kraken": "Kraken"}


def h(text: str | None) -> str:
    """Escape for Telegram HTML."""
    return escape(text or "", quote=False)


def ex_name(exchange: str) -> str:
    return EXCHANGE_NAMES.get(exchange, exchange.capitalize())


def fmt_price(value: float | None) -> str:
    if value is None:
        return "—"
    if value >= 1000:
        return f"{value:,.2f}"
    if value >= 1:
        return f"{value:,.4f}".rstrip("0").rstrip(".")
    return f"{value:.8f}".rstrip("0").rstrip(".")


def fmt_big(value: float | None) -> str:
    if value is None:
        return "—"
    for unit, size in (("T", 1e12), ("B", 1e9), ("M", 1e6), ("K", 1e3)):
        if value >= size:
            return f"{value / size:.1f}{unit}"
    return f"{value:,.0f}"


def fmt_pct(value: float | None, *, dot: bool = True) -> str:
    """'🟢 +1.23%' / '🔴 −1.23%' (real minus sign reads better)."""
    if value is None:
        return "—"
    text = f"{value:+.2f}%".replace("-", "−")
    return f"{'🟢' if value >= 0 else '🔴'} {text}" if dot else text


def utcnow() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def ago(ts: datetime | None) -> str:
    if ts is None:
        return ""
    minutes = int((utcnow() - ts).total_seconds() // 60)
    if minutes < 1:
        return "just now"
    if minutes < 60:
        return f"{minutes} min ago"
    if minutes < 48 * 60:
        return f"{minutes // 60} h ago"
    return f"{minutes // 1440} d ago"


def updated_line() -> str:
    return f"<i>Updated {utcnow():%H:%M:%S} UTC</i>"
