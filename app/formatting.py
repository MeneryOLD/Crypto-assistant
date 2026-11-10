"""Number and message formatting helpers."""

from __future__ import annotations

from html import escape

TELEGRAM_LIMIT = 4096


def fmt_price(value: float | None) -> str:
    if value is None:
        return "n/a"
    if value >= 1000:
        return f"${value:,.0f}"
    if value >= 1:
        return f"${value:,.2f}"
    if value >= 0.01:
        return f"${value:.4f}"
    return f"${value:.8f}".rstrip("0")


def fmt_big(value: float | None) -> str:
    if value is None:
        return "n/a"
    for unit, size in (("T", 1e12), ("B", 1e9), ("M", 1e6), ("K", 1e3)):
        if abs(value) >= size:
            return f"${value / size:.2f}{unit}"
    return f"${value:,.0f}"


def fmt_pct(value: float | None) -> str:
    if value is None:
        return "n/a"
    arrow = "🟢" if value >= 0 else "🔴"
    return f"{arrow} {value:+.2f}%"


def h(text: str | None) -> str:
    """Escape text for Telegram HTML parse mode."""
    return escape(text or "", quote=False)


def split_message(text: str, limit: int = TELEGRAM_LIMIT) -> list[str]:
    """Split text into Telegram-sized chunks, preferring paragraph/line boundaries."""
    chunks: list[str] = []
    while len(text) > limit:
        cut = text.rfind("\n\n", 0, limit)
        if cut < limit // 2:
            cut = text.rfind("\n", 0, limit)
        if cut < limit // 2:
            cut = text.rfind(" ", 0, limit)
        if cut <= 0:
            cut = limit
        chunks.append(text[:cut].rstrip())
        text = text[cut:].lstrip()
    if text:
        chunks.append(text)
    return chunks
