"""Every screen of the bot: builds (text, buttons). Commands and button presses both use these."""

from __future__ import annotations

import asyncio
from datetime import timedelta
from math import ceil

from aiogram.filters.callback_data import CallbackData
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup, KeyboardButton, ReplyKeyboardMarkup

from analysis.importance import level
from analysis.patterns import Pattern, patterns, related, same_story, topics_of
from bot.formatting import CATEGORY_ICONS, ago, ex_name, fmt_big, fmt_pct, fmt_price, h, updated_line
from collectors.exchanges import ExchangeError, ExchangeHub, Ticker, best_spread
from collectors.fear_greed import FearGreed, FearGreedClient
from collectors.news import SOURCES, NewsCollector
from storage.db import HORIZONS as MARK_HORIZONS
from storage.db import Database, NewsItem, User

Screen = tuple[str, InlineKeyboardMarkup | None]

# --- navigation --------------------------------------------------------------

BTN_NEWS, BTN_TOP, BTN_MARKETS = "📰 News", "🔥 Top", "💹 Markets"
BTN_HISTORY, BTN_ALERTS, BTN_STATUS = "🧠 History", "🔔 Alerts", "📡 Status"

MAIN_MENU = ReplyKeyboardMarkup(
    keyboard=[
        [KeyboardButton(text=BTN_TOP), KeyboardButton(text=BTN_NEWS)],
        [KeyboardButton(text=BTN_MARKETS), KeyboardButton(text=BTN_HISTORY)],
        [KeyboardButton(text=BTN_ALERTS), KeyboardButton(text=BTN_STATUS)],
    ],
    resize_keyboard=True,
    is_persistent=True,
    input_field_placeholder="Type a coin (btc) or search news (trump, fed…)",
)

NEWS_TABS = [
    ("all", "All"),
    ("crypto", "🪙 Crypto"),
    ("politics", "🏛 Politics"),
    ("elections", "🗳 Elections"),
    ("economy", "📊 Economy"),
    ("regulation", "⚖️ Rules"),
]
NEWS_TABS_KEYS = [k for k, _ in NEWS_TABS if k != "all"]
COINS = ["BTC", "ETH", "SOL", "BNB", "XRP", "DOGE"]
FAST_SOURCES = {s.name for s in SOURCES if s.kind == "telegram"}
HORIZONS = [(15, "15m"), (60, "1h"), (240, "4h"), (1440, "24h")]
PAGE_SIZE = 6
MAX_PAGES = 8


class NewsCB(CallbackData, prefix="nw"):
    cat: str = "all"
    page: int = 0
    q: str = ""
    s: str = "new"  # new = latest first | top = most important first


class MarketCB(CallbackData, prefix="mk"):
    view: str = "coin"  # coin | book | arb
    coin: str = "BTC"
    ex: str = ""


class ItemCB(CallbackData, prefix="it"):
    id: int
    cat: str = "all"  # where "Back" returns to
    page: int = 0
    q: str = ""
    s: str = "new"
    new: int = 0  # 1 = open as a new message (from an alert)


class AlertsCB(CallbackData, prefix="al"):
    action: str = "show"  # show | on | off | level | cat | test
    value: str = ""


class HistoryCB(CallbackData, prefix="hi"):
    cat: str = "all"
    hz: int = 60
    mode: str = "ev"  # ev = biggest moves (events) | tp = by topic | ct = by category


class StatusCB(CallbackData, prefix="st"):
    full: bool = False


def _tab(label: str, selected: bool) -> str:
    return f"• {label} •" if selected else label


def _rows(buttons: list[InlineKeyboardButton], per_row: int) -> list[list[InlineKeyboardButton]]:
    return [buttons[i : i + per_row] for i in range(0, len(buttons), per_row)]


def safe_query(q: str) -> str:
    """Search text that fits into button data (64 bytes, no ':')."""
    return q.replace(":", " ").strip()[:24]


# --- home --------------------------------------------------------------------


def fear_greed_line(fg: FearGreed | None) -> str:
    if fg is None:
        return ""
    trend = ""
    if fg.yesterday is not None:
        diff = fg.value - fg.yesterday
        trend = f" ({'+' if diff >= 0 else '−'}{abs(diff)} vs yesterday)"
    return f"{fg.emoji} Fear &amp; Greed: <b>{fg.value}</b> {h(fg.label)}{trend}"


def _source_icon(name: str) -> str:
    return "⚡ " if name in FAST_SOURCES else ""


async def home(
    db: Database,
    hub: ExchangeHub,
    collector: NewsCollector,
    fg: FearGreedClient,
    first_name: str | None,
    user: User | None = None,
) -> str:
    btc, eth, fear_greed = await asyncio.gather(
        hub.ticker(hub.names[0], "BTC"), hub.ticker(hub.names[0], "ETH"), fg.get(), return_exceptions=True
    )
    prices = []
    for coin, t in (("BTC", btc), ("ETH", eth)):
        if isinstance(t, Ticker):
            prices.append(f"<b>{coin}</b> ${fmt_price(t.last)}  {fmt_pct(t.change_pct)}")
    news_24h = await db.count_news(hours=24)
    online = sum(1 for s in collector.status.values() if s.ok)
    top = dedupe_stories(await db.top_news(hours=6, limit=40))[:4]
    if not top:
        top = await db.recent_news(limit=4)

    lines = [f"👋 <b>Hi{', ' + h(first_name) if first_name else ''}! This is Vexora</b>", "<i>Your crypto & world news radar</i>", ""]
    lines += prices or ["<i>Prices unavailable right now</i>"]
    if isinstance(fear_greed, FearGreed):
        lines.append(fear_greed_line(fear_greed))
    lines += ["", f"📰 <b>{news_24h:,}</b> news in 24 h · 📡 {online}/{len(collector.sources)} sources online"]
    if user is not None:
        if user.alerts_on:
            name = next((n for lvl, n, _ in ALERT_LEVELS if lvl == user.alert_level), f"{user.alert_level}+")
            lines.append(f"🔔 Alerts on: <b>{h(name)}</b> — change in {BTN_ALERTS}")
        else:
            lines.append(f"🔕 Alerts are off — turn on in {BTN_ALERTS}")
    if top:
        lines += ["", "🔥 <b>Most important right now</b>"]
        lines += [
            f"{badge(n) or CATEGORY_ICONS.get(n.category, '•')} {_source_icon(n.source)}<a href=\"{h(n.url)}\">{h(n.title)}</a>"
            for n in top
        ]
    lines += ["", "Use the buttons below 👇  or just type a coin (<code>sol</code>) or a word (<code>fed</code>)."]
    return "\n".join(lines)


HELP = (
    "<b>How to use Vexora</b>\n\n"
    f"{BTN_TOP} — the most important news of the last 24 h, one line per story\n"
    f"{BTN_NEWS} — world news by category; tap a number to open the story\n"
    f"{BTN_ALERTS} — get important news the second it's published; choose level and categories\n"
    f"{BTN_MARKETS} — prices, funding rates and order books on Binance, Bybit, OKX, Kraken\n"
    f"{BTN_HISTORY} — how BTC moved after news, and average patterns by topic\n"
    f"{BTN_STATUS} — are all sources and exchanges working\n\n"
    "<b>Type anything:</b>\n"
    "<code>btc</code>, <code>sol</code> → that coin's prices\n"
    "<code>trump</code>, <code>fed</code>, <code>ETF</code> → search the news\n\n"
    "⚡ marks fast Telegram sources (seconds after events)\n"
    "🔴 9–10 critical · 🟠 7–8 important · 🟡 5–6 notable · ⚪ minor\n"
    "<i>Commands also work: /news /markets /history /status</i>"
)


# --- news --------------------------------------------------------------------


def badge(item: NewsItem) -> str:
    """'🟠 7' importance badge (empty while not scored yet)."""
    if item.importance is None:
        return ""
    emoji, _ = level(item.importance)
    return f"{emoji} {item.importance}"


def dedupe_stories(items: list[NewsItem]) -> list[NewsItem]:
    """Keep only the first (highest-ranked) item of each story."""
    kept: list[NewsItem] = []
    for item in items:
        if not any(same_story(item, k) for k in kept):
            kept.append(item)
    return kept


async def news(db: Database, cat: str = "all", page: int = 0, q: str = "", s: str = "new") -> Screen:
    category = None if cat == "all" else cat
    top = s == "top" and not q
    if top:
        ranked = dedupe_stories(await db.top_news(hours=24, category=category, limit=150))[: PAGE_SIZE * MAX_PAGES]
        pages = max(1, ceil(len(ranked) / PAGE_SIZE))
        page = max(0, min(page, pages - 1))
        items = ranked[page * PAGE_SIZE : (page + 1) * PAGE_SIZE]
    else:
        hours = 24 * 7 if q else 48
        total = min(await db.count_news(category=category, query=q or None, hours=hours), PAGE_SIZE * MAX_PAGES)
        pages = max(1, ceil(total / PAGE_SIZE))
        page = max(0, min(page, pages - 1))
        items = await db.recent_news(
            limit=PAGE_SIZE, offset=page * PAGE_SIZE, category=category, query=q or None, hours=hours
        )

    if q:
        title = f"🔎 <b>News about “{h(q)}”</b>"
    elif top:
        label = dict(NEWS_TABS)[cat] if cat != "all" else "all categories"
        title = f"🔥 <b>Most important · 24 h</b> <i>({label})</i>"
    else:
        label = dict(NEWS_TABS)[cat] if cat != "all" else "📰 All news"
        title = f"<b>{label}</b> <i>· latest</i>"
    lines = [f"{title}  <i>· page {page + 1}/{pages}</i>", ""]
    if not items:
        lines.append("Nothing here yet. News is checked every few seconds — tap 🔄 in a bit.")
    for i, n in enumerate(items, 1):
        icon = CATEGORY_ICONS.get(n.category, "•")
        mark = badge(n) if (top or (n.importance or 0) >= 5) else ""
        lines.append(
            f"<b>{i}.</b> {icon} <b><a href=\"{h(n.url)}\">{h(n.title)}</a></b>\n"
            f"<i>{mark + ' · ' if mark else ''}{_source_icon(n.source)}{h(n.source)} · {ago(n.event_at)}</i>\n"
        )
    if items:
        lines.append("<i>Tap a number for details, why it matters and the BTC reaction.</i>")

    kb: list[list[InlineKeyboardButton]] = []
    if items:
        kb.append(
            [
                InlineKeyboardButton(text=str(i), callback_data=ItemCB(id=n.id, cat=cat, page=page, q=q, s=s).pack())
                for i, n in enumerate(items, 1)
            ]
        )
    if not q:
        kb.append(
            [
                InlineKeyboardButton(text=_tab("🕒 Latest", not top), callback_data=NewsCB(cat=cat, s="new").pack()),
                InlineKeyboardButton(text=_tab("🔥 Most important", top), callback_data=NewsCB(cat=cat, s="top").pack()),
            ]
        )
        tabs = [
            InlineKeyboardButton(text=_tab(label, key == cat), callback_data=NewsCB(cat=key, s=s).pack())
            for key, label in NEWS_TABS
        ]
        kb += _rows(tabs, 3)
    nav = []
    if page > 0:
        nav.append(InlineKeyboardButton(text="◀️", callback_data=NewsCB(cat=cat, page=page - 1, q=q, s=s).pack()))
    nav.append(InlineKeyboardButton(text="🔄", callback_data=NewsCB(cat=cat, page=page, q=q, s=s).pack()))
    if page < pages - 1:
        nav.append(InlineKeyboardButton(text="▶️", callback_data=NewsCB(cat=cat, page=page + 1, q=q, s=s).pack()))
    kb.append(nav)
    if q:
        kb.append([InlineKeyboardButton(text="📰 All news", callback_data=NewsCB().pack())])
    return "\n".join(lines).rstrip(), InlineKeyboardMarkup(inline_keyboard=kb)


def _reaction_table(marks: dict[tuple[str, int], float]) -> str | None:
    labels = {0: "at news", 15: "+15m", 60: "+1h", 240: "+4h", 1440: "+24h"}
    rows = [("", "BTC", "ETH")]
    for m in MARK_HORIZONS:
        cells = []
        for asset in ("BTC", "ETH"):
            base, price = marks.get((asset, 0)), marks.get((asset, m))
            if price is None or base is None:
                cells.append("…")
            elif m == 0:
                cells.append(fmt_price(price))
            else:
                cells.append(f"{(price - base) / base * 100:+.2f}%".replace("-", "−"))
        rows.append((labels[m], *cells))
    if all(c == "…" for r in rows[1:] for c in r[1:]):
        return None
    widths = [max(len(r[i]) for r in rows) for i in range(3)]
    text = "\n".join(f"{r[0]:<{widths[0]}}  {r[1]:>{widths[1]}}  {r[2]:>{widths[2]}}" for r in rows)
    return f"<pre>{h(text)}</pre>"


async def article(db: Database, news_id: int, cat: str = "all", page: int = 0, q: str = "", s: str = "new") -> Screen:
    back = InlineKeyboardButton(text="◀️ Back to list", callback_data=NewsCB(cat=cat, page=page, q=q, s=s).pack())
    item = await db.get_news(news_id)
    if item is None:
        return "This story is no longer stored.", InlineKeyboardMarkup(inline_keyboard=[[back]])

    marks, nearby = await asyncio.gather(
        db.marks_for(item.id),
        db.news_between(item.event_at - timedelta(hours=24), item.event_at + timedelta(hours=24)),
    )
    others = related(item, nearby)
    topics = topics_of(item)
    published = f"{item.event_at:%d %b %H:%M} UTC · {ago(item.event_at)}"

    lines = [
        f"{CATEGORY_ICONS.get(item.category, '•')} <b>{h(item.title)}</b>",
        "",
        f"<i>{_source_icon(item.source)}{h(item.source)} · {published}</i>",
    ]
    if item.importance is not None:
        emoji, label = level(item.importance)
        lines.append(f"{emoji} <b>{label} · {item.importance}/10</b>")
        if item.importance_reasons:
            lines.append(f"<i>Why: {h(item.importance_reasons)}</i>")
    if topics:
        lines.append("Topics: " + " · ".join(topics))
    if item.summary:
        summary = item.summary if len(item.summary) <= 700 else item.summary[:699].rsplit(" ", 1)[0] + "…"
        lines += ["", f"<blockquote expandable>{h(summary)}</blockquote>"]
    table = _reaction_table(marks)
    lines += ["", "📈 <b>Price after this news</b>"]
    lines.append(table or "<i>Recorded within 2 minutes of publication, then at +15m, +1h, +4h, +24h.</i>")
    if others:
        lines += ["", f"🗞 <b>Also reported by {len(others)} other outlet{'s' if len(others) > 1 else ''}</b>"]
        lines += [f"• <a href=\"{h(o.url)}\">{h(o.source)}</a> · {ago(o.event_at)}" for o in others]
        earliest = min([item, *others], key=lambda n: n.event_at)
        if earliest.id != item.id:
            lines.append(f"<i>First reported by {h(earliest.source)}</i>")

    kb = [[InlineKeyboardButton(text="🔗 Open article", url=item.url)], [back]]
    return "\n".join(lines), InlineKeyboardMarkup(inline_keyboard=kb)


# --- markets -----------------------------------------------------------------


def _coin_tabs(selected: str, view: str = "coin") -> list[list[InlineKeyboardButton]]:
    coins = COINS if selected in COINS else [selected, *COINS[:5]]
    return _rows(
        [
            InlineKeyboardButton(text=_tab(c, c == selected), callback_data=MarketCB(view=view, coin=c).pack())
            for c in coins
        ],
        6,
    )


def _pre_table(rows: list[tuple[str, ...]]) -> str:
    widths = [max(len(r[i]) for r in rows) for i in range(len(rows[0]))]
    out = []
    for r in rows:
        cells = [r[0].ljust(widths[0])] + [c.rjust(w) for c, w in zip(r[1:], widths[1:])]
        out.append("  ".join(cells))
    return "<pre>" + h("\n".join(out)) + "</pre>"


def _funding_line(rates: dict[str, float]) -> str | None:
    if not rates:
        return None
    avg = sum(rates.values()) / len(rates) * 100
    if avg > 0.03:
        mood = "longs crowded — pay a lot to stay long"
    elif avg > 0:
        mood = "slightly more longs than shorts"
    elif avg > -0.01:
        mood = "slightly more shorts than longs"
    else:
        mood = "shorts crowded — squeeze risk"
    parts = " · ".join(f"{ex_name(e)} {r * 100:+.4f}%".replace("-", "−") for e, r in rates.items())
    return f"⏱ Funding (8h): {parts}\n<i>{mood}</i>"


async def market(hub: ExchangeHub, coin: str = "BTC", fg: FearGreedClient | None = None) -> Screen:
    coin = coin.upper()
    (tickers, errors), rates, fear_greed = await asyncio.gather(
        hub.tickers_everywhere(coin),
        hub.funding_rates(coin),
        fg.get() if fg else asyncio.sleep(0, result=None),
    )
    kb = _coin_tabs(coin)
    if not tickers:
        text = f"⚠️ <b>{h(coin)}</b> isn't available on {', '.join(ex_name(e) for e in hub.names)} right now."
        return text, InlineKeyboardMarkup(inline_keyboard=kb)

    main = max(tickers, key=lambda t: t.quote_volume or 0)
    rows = [("Exchange", "Price", "24h")] + [
        (ex_name(t.exchange), fmt_price(t.last), fmt_pct(t.change_pct, dot=False))
        for t in sorted(tickers, key=lambda t: -(t.quote_volume or 0))
    ]
    lo, hi = min(t.last for t in tickers), max(t.last for t in tickers)
    volume = sum(t.quote_volume or 0 for t in tickers)

    lines = [
        f"💹 <b>{h(coin)}</b>   <b>${fmt_price(main.last)}</b>   {fmt_pct(main.change_pct)}",
        f"<i>24h range {fmt_price(main.low)} – {fmt_price(main.high)} on {ex_name(main.exchange)}</i>",
        _pre_table(rows),
        f"Volume 24h: <b>${fmt_big(volume)}</b>",
    ]
    if len(tickers) > 1:
        lines.append(f"Gap between exchanges: <b>{(hi - lo) / lo * 100:.3f}%</b>")
    if funding := _funding_line(rates):
        lines += ["", funding]
    if coin == "BTC" and fear_greed:
        lines.append(fear_greed_line(fear_greed))
    if errors:
        lines.append(f"<i>Not available: {', '.join(ex_name(e) for e in errors)}</i>")
    lines.append(updated_line())

    kb.append(
        [
            InlineKeyboardButton(text="📖 Order book", callback_data=MarketCB(view="book", coin=coin, ex=main.exchange).pack()),
            InlineKeyboardButton(text="⚖️ Buy/sell gap", callback_data=MarketCB(view="arb", coin=coin).pack()),
        ]
    )
    kb.append([InlineKeyboardButton(text="🔄 Refresh", callback_data=MarketCB(coin=coin).pack())])
    return "\n".join(lines), InlineKeyboardMarkup(inline_keyboard=kb)


async def order_book(hub: ExchangeHub, coin: str, exchange: str) -> Screen:
    coin = coin.upper()
    exchange = hub.find(exchange) or hub.names[0]
    tabs = [
        InlineKeyboardButton(text=_tab(ex_name(e), e == exchange), callback_data=MarketCB(view="book", coin=coin, ex=e).pack())
        for e in hub.names
    ]
    kb = _rows(tabs, 4) + [
        [
            InlineKeyboardButton(text="◀️ Back", callback_data=MarketCB(coin=coin).pack()),
            InlineKeyboardButton(text="🔄 Refresh", callback_data=MarketCB(view="book", coin=coin, ex=exchange).pack()),
        ]
    ]
    try:
        ob = await hub.order_book(exchange, coin, depth=6)
    except ExchangeError as exc:
        return f"⚠️ {h(str(exc))}", InlineKeyboardMarkup(inline_keyboard=kb)

    rows = [("", "Price", "Amount")]
    rows += [("sell", fmt_price(p), f"{a:,.4f}") for p, a in reversed(ob.asks)]
    rows += [("buy", fmt_price(p), f"{a:,.4f}") for p, a in ob.bids]
    spread = ""
    if ob.bids and ob.asks:
        pct = (ob.asks[0][0] - ob.bids[0][0]) / ob.asks[0][0] * 100
        spread = f"Spread: <b>{'&lt;0.001' if pct < 0.001 else f'{pct:.3f}'}%</b>  "
    text = (
        f"📖 <b>{h(ob.symbol)}</b> on <b>{ex_name(exchange)}</b>\n"
        f"<i>🔴 sellers (asks) above · 🟢 buyers (bids) below</i>\n"
        f"{_pre_table(rows)}\n{spread}{updated_line()}"
    )
    return text, InlineKeyboardMarkup(inline_keyboard=kb)


async def buy_sell_gap(hub: ExchangeHub, coin: str) -> Screen:
    coin = coin.upper()
    kb = [
        [
            InlineKeyboardButton(text="◀️ Back", callback_data=MarketCB(coin=coin).pack()),
            InlineKeyboardButton(text="🔄 Refresh", callback_data=MarketCB(view="arb", coin=coin).pack()),
        ]
    ]
    tickers, errors = await hub.tickers_everywhere(coin)
    s = best_spread(tickers)
    if s is None:
        return f"⚠️ Need <b>{h(coin)}</b> prices from at least 2 exchanges.", InlineKeyboardMarkup(inline_keyboard=kb)
    lines = [f"⚖️ <b>{h(coin)} — cheapest buy vs. best sell</b>", ""]
    if s.pct <= 0:
        lines.append("No gap right now — prices are in line across exchanges.")
    else:
        lines += [
            f"🛒 Buy on <b>{ex_name(s.buy.exchange)}</b>: ${fmt_price(s.buy.ask)}",
            f"💰 Sell on <b>{ex_name(s.sell.exchange)}</b>: ${fmt_price(s.sell.bid)}",
            f"Gap: <b>{s.pct:.3f}%</b>",
            "",
            "<i>Fees are ~0.1–0.2% per trade plus transfer costs, so small gaps aren't real profit.</i>",
        ]
    if errors:
        lines.append(f"<i>Not included: {', '.join(ex_name(e) for e in errors)}</i>")
    lines.append(updated_line())
    return "\n".join(lines), InlineKeyboardMarkup(inline_keyboard=kb)


# --- history -----------------------------------------------------------------

HISTORY_MODES = [("ev", "📋 Biggest moves"), ("tp", "🏷 By topic"), ("ct", "🗂 By category")]


def _history_mode_tabs(mode: str, cat: str, hz: int) -> list[InlineKeyboardButton]:
    return [
        InlineKeyboardButton(text=_tab(label, key == mode), callback_data=HistoryCB(cat=cat, hz=hz, mode=key).pack())
        for key, label in HISTORY_MODES
    ]


def _pattern_line(p: Pattern) -> str:
    moves = " · ".join(
        f"{dict(HORIZONS)[m]} {fmt_pct(p.avg[m])}" for m in (60, 240, 1440) if m in p.avg
    ) or "no moves recorded yet"
    up = f" · ↑ {p.up_share[240] * 100:.0f}% of the time (4h)" if 240 in p.up_share else ""
    weak = " <i>(few events)</i>" if p.events < 5 else ""
    return f"<b>{h(p.name)}</b> · {p.events} event{'s' if p.events != 1 else ''}{weak}\n{moves}{up}"


async def history(db: Database, cat: str = "all", hz: int = 60, mode: str = "ev") -> Screen:
    if mode in ("tp", "ct"):
        return await history_patterns(db, mode)
    category = None if cat == "all" else cat
    reactions = await db.reactions(category=category, horizon=hz, limit=8)
    hz_label = dict(HORIZONS)[hz]

    lines = [
        "🧠 <b>Memory: what BTC did after the news</b>",
        f"<i>Biggest BTC moves {hz_label} after a news item · last 7 days</i>",
        "",
    ]
    if not reactions:
        lines.append(
            f"Nothing yet for this view. Prices are recorded at publication and +15m, +1h, +4h, +24h — "
            f"the {hz_label} numbers appear once that much time has passed."
        )
    for r in reactions:
        n = r.item
        icon = CATEGORY_ICONS.get(n.category, "•")
        lines.append(
            f"{fmt_pct(r.changes[hz])}  {icon} <a href=\"{h(n.url)}\">{h(n.title)}</a>\n"
            f"<i>{_source_icon(n.source)}{h(n.source)} · {ago(n.event_at)} · BTC was ${fmt_price(r.price_0)}</i>\n"
        )
    lines.append("<i>Shows what happened after the news — not proof the news caused it.</i>")

    cat_tabs = [
        InlineKeyboardButton(text=_tab(label, key == cat), callback_data=HistoryCB(cat=key, hz=hz).pack())
        for key, label in NEWS_TABS
    ]
    hz_tabs = [
        InlineKeyboardButton(text=_tab(label, m == hz), callback_data=HistoryCB(cat=cat, hz=m).pack())
        for m, label in HORIZONS
    ]
    kb = [_history_mode_tabs("ev", cat, hz)] + _rows(cat_tabs, 3) + [hz_tabs]
    return "\n".join(lines), InlineKeyboardMarkup(inline_keyboard=kb)


async def history_patterns(db: Database, mode: str) -> Screen:
    reactions = await db.all_reactions(days=30)
    baseline, groups = patterns(reactions, by="topic" if mode == "tp" else "category")
    if mode == "ct":
        labels = dict(NEWS_TABS)
        groups = [Pattern(labels.get(g.name, g.name), g.events, g.avg, g.up_share, g.samples) for g in groups]

    lines = [
        "🧠 <b>Patterns: average BTC move after each kind of news</b>",
        "<i>Last 30 days · news in the same 30 minutes counted once</i>",
        "",
    ]
    if baseline is None:
        lines.append("Not enough history yet — patterns build up as the bot runs.")
    else:
        lines += [_pattern_line(Pattern("📊 Any news (baseline)", baseline.events, baseline.avg, baseline.up_share, baseline.samples)), ""]
        shown = [g for g in groups if g.events >= 2][:12]
        lines += [_pattern_line(g) + "\n" for g in shown]
        if not shown:
            lines.append("No topic has enough events yet.")
        lines.append(
            "<i>Compare each line with the baseline: a topic matters if BTC moves differently after it. "
            "The more events, the more reliable. Signals, not guarantees.</i>"
        )
    kb = [_history_mode_tabs(mode, "all", 60)]
    return "\n".join(lines).rstrip(), InlineKeyboardMarkup(inline_keyboard=kb)


# --- status ------------------------------------------------------------------


async def status(db: Database, hub: ExchangeHub, collector: NewsCollector, full: bool = False) -> Screen:
    ex_statuses, stats, marks = await asyncio.gather(hub.status(), db.news_stats(), db.count_price_marks())
    sources = collector.sources
    online = [s for s in sources if collector.status[s.name].ok]
    failed = [s for s in sources if collector.status[s.name].ok is False]

    lines = ["📡 <b>Status</b>", "", "<b>Exchanges</b>"]
    for s in ex_statuses:
        lines.append(f"{'✅' if s.ok else '❌'} {ex_name(s.exchange)}" + (f" · {s.latency_ms} ms" if s.ok else f" · {h(s.error)}"))

    lines += ["", f"<b>News sources</b>: {len(online)}/{len(sources)} online"]
    groups: dict[str, list] = {}
    for s in sources:
        groups.setdefault(s.group, []).append(s)
    for group, members in groups.items():
        ok = sum(1 for s in members if collector.status[s.name].ok)
        every = members[0].every
        interval = f"every {every} s" if every < 60 else f"every {every // 60} min"
        lines.append(f"• {h(group)}: <b>{ok}/{len(members)}</b> · {interval}")
        if full:
            names = ", ".join(
                ("❌" if collector.status[s.name].ok is False else "") + h(s.name.removeprefix("Google News: "))
                for s in members
            )
            lines.append(f"  <i>{names}</i>")
    if failed and not full:
        lines += ["", "<b>Not responding</b>"]
        lines += [f"❌ {h(s.name)}: {h(collector.status[s.name].error)}" for s in failed[:10]]

    total = sum(stats.values())
    lines += ["", "<b>Memory</b>", f"🗂 {total:,} news items · 📈 {marks:,} price points"]
    if total:
        lines.append(" · ".join(f"{CATEGORY_ICONS.get(c, '')} {n:,}" for c, n in sorted(stats.items(), key=lambda x: -x[1])))
    lines.append(updated_line())

    kb = [
        [
            InlineKeyboardButton(
                text="Hide source names" if full else "Show all sources", callback_data=StatusCB(full=not full).pack()
            ),
            InlineKeyboardButton(text="🔄 Refresh", callback_data=StatusCB(full=full).pack()),
        ]
    ]
    return "\n".join(lines), InlineKeyboardMarkup(inline_keyboard=kb)


# --- alerts ------------------------------------------------------------------

ALERT_LEVELS = [
    (9, "🔴 Critical only", "wars, rate decisions, collapses, ETF decisions"),
    (7, "🟠 Important +", "also key economic data, regulator actions, big hacks, sanctions"),
    (5, "🟡 Notable +", "also big market moves and notable politics (more messages)"),
]


def alert_message(item: NewsItem, btc: Ticker | None, pattern: Pattern | None) -> str:
    emoji, label = level(item.importance)
    topics = topics_of(item)
    head = f"{emoji} <b>{label.upper()} · {item.importance}/10</b>"
    if topics:
        head += f"   {topics[0]}"
    lines = [
        head,
        "",
        f"<b>{h(item.title)}</b>",
        f"<i>{_source_icon(item.source)}{h(item.source)} · {item.event_at:%H:%M} UTC ({ago(item.event_at)})</i>",
    ]
    if item.importance_reasons:
        lines.append(f"<i>Why: {h(item.importance_reasons)}</i>")
    lines.append("")
    if btc:
        lines.append(f"₿ BTC now <b>${fmt_price(btc.last)}</b> ({fmt_pct(btc.change_pct)} 24h)")
    if pattern:
        avg4 = pattern.avg[240]
        up = pattern.up_share.get(240, 0) * 100
        lines.append(
            f"📊 After similar news ({h(pattern.name)}, {pattern.events} events): "
            f"BTC {fmt_pct(avg4, dot=False)} on average in 4h, up {up:.0f}% of the time"
        )
    return "\n".join(lines).rstrip()


def alert_buttons(item: NewsItem) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="📖 Details", callback_data=ItemCB(id=item.id, s="top", new=1).pack()),
                InlineKeyboardButton(text="🔗 Open", url=item.url),
            ],
            [InlineKeyboardButton(text="🔔 Alert settings", callback_data=AlertsCB().pack())],
        ]
    )


async def alerts_settings(db: Database, user: User) -> Screen:
    cats = user.categories
    would = await db.count_matching(user.alert_level, cats, hours=24)
    level_name = next((name for lvl, name, _ in ALERT_LEVELS if lvl == user.alert_level), f"{user.alert_level}+")
    cat_names = ", ".join(dict(NEWS_TABS)[c] for c in NEWS_TABS_KEYS if c in cats) if cats else "all categories"

    lines = ["🔔 <b>Notifications</b>", ""]
    if user.alerts_on:
        lines += [
            "Status: <b>ON</b> ✅",
            f"You get: <b>{h(level_name)}</b> from <b>{h(cat_names)}</b>",
            f"<i>With these settings you'd have received ~{would} alert{'s' if would != 1 else ''} in the last 24 h "
            "(repeats of the same story are skipped, max 12 per hour).</i>",
        ]
    else:
        lines += ["Status: <b>OFF</b> 🔕", "<i>Turn on to get important news the moment it's published.</i>"]
    lines += ["", "<b>Levels</b>"]
    lines += [f"{name} — <i>{desc}</i>" for _, name, desc in ALERT_LEVELS]
    lines += ["", "Every news item gets a 1–10 score from: source · type of event · money involved · "
              "crypto relevance · how many outlets confirm it."]

    toggle = (
        InlineKeyboardButton(text="🔕 Turn off", callback_data=AlertsCB(action="off").pack())
        if user.alerts_on
        else InlineKeyboardButton(text="🔔 Turn on", callback_data=AlertsCB(action="on").pack())
    )
    level_row = [
        InlineKeyboardButton(text=_tab(name, lvl == user.alert_level), callback_data=AlertsCB(action="level", value=str(lvl)).pack())
        for lvl, name, _ in ALERT_LEVELS
    ]
    cat_buttons = [
        InlineKeyboardButton(
            text=f"{'✅' if (not cats or key in cats) else '⬜'} {dict(NEWS_TABS)[key]}",
            callback_data=AlertsCB(action="cat", value=key).pack(),
        )
        for key in NEWS_TABS_KEYS
    ]
    kb = [[toggle], level_row, *_rows(cat_buttons, 3)]
    kb.append(
        [
            InlineKeyboardButton(text="🧪 Send test alert", callback_data=AlertsCB(action="test").pack()),
            InlineKeyboardButton(text="🔥 Top news", callback_data=NewsCB(s="top").pack()),
        ]
    )
    return "\n".join(lines), InlineKeyboardMarkup(inline_keyboard=kb)


def toggle_category(current: set[str], key: str) -> set[str]:
    """Empty set means 'all'. Toggling from 'all' keeps everything except the tapped one."""
    selected = set(current) or set(NEWS_TABS_KEYS)
    selected ^= {key}
    if not selected or selected == set(NEWS_TABS_KEYS):
        return set()
    return selected
