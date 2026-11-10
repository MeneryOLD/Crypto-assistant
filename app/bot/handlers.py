"""Telegram command handlers. Thin layer: parse input, call services, format output."""

from __future__ import annotations

import logging
import time

from aiogram import F, Router
from aiogram.enums import ChatAction
from aiogram.filters import Command, CommandObject, CommandStart
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message
from sqlalchemy.ext.asyncio import AsyncSession

from app import services
from app.config import Settings
from app.formatting import fmt_big, fmt_pct, fmt_price, h, split_message
from app.market.coingecko import CoinGeckoClient, CoinResolver, MarketDataError
from app.models import AlertKind, User
from app.news.collector import recent_news
from app.research.assistant import ResearchAssistant, ResearchUnavailable
from app.research.evidence import build_evidence

log = logging.getLogger(__name__)
router = Router(name="main")

HELP_TEXT = """<b>Crypto Intelligence</b> — market research assistant

<b>Market</b>
/market — market overview (top coins, dominance)
/price btc eth — current prices

<b>News</b>
/news — latest collected news
/news eth — news about a coin

<b>AI research</b>
/research Why is Bitcoin falling today?
/research Bull and bear case for ETH this week

<b>Watchlist</b>
/watchlist — show your coins
/watchlist add sol — follow a coin (you get news that mentions it)
/watchlist remove sol

<b>Alerts</b>
/alerts — list active alerts
/alerts add btc above 120000
/alerts add eth below 2500
/alerts add sol change 8  (|24h change| ≥ 8%)
/alerts remove 3

Research is AI-generated and is not financial advice."""

_research_last_call: dict[int, float] = {}


# --- basics ----------------------------------------------------------------


@router.message(CommandStart())
async def cmd_start(message: Message, user: User) -> None:
    await message.answer(
        f"Hello, {h(user.first_name or 'trader')}! Your profile has been saved.\n\n{HELP_TEXT}"
    )


@router.message(Command("help"))
async def cmd_help(message: Message) -> None:
    await message.answer(HELP_TEXT)


# --- market ----------------------------------------------------------------


def _market_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="🔄 Refresh", callback_data="market:refresh")]])


async def _market_overview(cg: CoinGeckoClient, count: int) -> str:
    g = await cg.global_market()
    top = await cg.top_markets(count)
    lines = [
        "<b>Market overview</b>",
        f"Total cap: {fmt_big(g.total_market_cap_usd)} ({fmt_pct(g.market_cap_change_24h)})",
        f"24h volume: {fmt_big(g.total_volume_usd)}",
        f"Dominance: BTC {g.btc_dominance:.1f}% · ETH {g.eth_dominance:.1f}%",
        "",
    ]
    for m in top:
        lines.append(f"{m.rank or '-'}. <b>{h(m.symbol)}</b> {fmt_price(m.price_usd)}  {fmt_pct(m.change_24h)}")
    movers = sorted((m for m in top if m.change_24h is not None), key=lambda m: m.change_24h or 0)
    if movers:
        lines += [
            "",
            f"Top gainer: {h(movers[-1].symbol)} {fmt_pct(movers[-1].change_24h)}",
            f"Top loser: {h(movers[0].symbol)} {fmt_pct(movers[0].change_24h)}",
        ]
    lines.append("\n<i>Source: CoinGecko</i>")
    return "\n".join(lines)


@router.message(Command("market"))
async def cmd_market(message: Message, cg: CoinGeckoClient, settings: Settings) -> None:
    try:
        text = await _market_overview(cg, settings.tracked_coins_count)
    except MarketDataError as exc:
        log.warning("market overview failed: %s", exc)
        await message.answer("Market data is temporarily unavailable. Please try again shortly.")
        return
    await message.answer(text, reply_markup=_market_keyboard())


@router.callback_query(F.data == "market:refresh")
async def cb_market_refresh(query: CallbackQuery, cg: CoinGeckoClient, settings: Settings) -> None:
    try:
        text = await _market_overview(cg, settings.tracked_coins_count)
    except MarketDataError:
        await query.answer("Market data unavailable, try again shortly.", show_alert=True)
        return
    if isinstance(query.message, Message):
        try:
            await query.message.edit_text(text, reply_markup=_market_keyboard())
        except Exception:  # "message is not modified" when nothing changed
            pass
    await query.answer("Updated")


@router.message(Command("price"))
async def cmd_price(message: Message, command: CommandObject, cg: CoinGeckoClient, resolver: CoinResolver) -> None:
    queries = (command.args or "btc eth").replace(",", " ").split()[:10]
    refs, unknown = [], []
    try:
        for q in queries:
            ref = await resolver.resolve(q)
            (refs.append(ref) if ref else unknown.append(q))
        markets = await cg.markets_for([r.coin_id for r in refs])
    except MarketDataError:
        await message.answer("Market data is temporarily unavailable. Please try again shortly.")
        return

    lines = []
    for m in markets:
        lines.append(
            f"<b>{h(m.name)} ({h(m.symbol)})</b>\n"
            f"Price: {fmt_price(m.price_usd)}\n"
            f"1h {fmt_pct(m.change_1h)} · 24h {fmt_pct(m.change_24h)} · 7d {fmt_pct(m.change_7d)}\n"
            f"Market cap: {fmt_big(m.market_cap)} · Vol 24h: {fmt_big(m.volume_24h)}"
        )
    if unknown:
        lines.append("Unknown coin(s): " + ", ".join(h(u) for u in unknown))
    await message.answer("\n\n".join(lines) or "No data found.")


# --- news ------------------------------------------------------------------


@router.message(Command("news"))
async def cmd_news(message: Message, command: CommandObject, session: AsyncSession, resolver: CoinResolver) -> None:
    terms = None
    title = "Latest news"
    if command.args:
        try:
            ref = await resolver.resolve(command.args)
        except MarketDataError:
            ref = None
        terms = [ref.name, ref.symbol] if ref else [command.args.strip()]
        title = f"News: {ref.name if ref else command.args.strip()}"

    items = await recent_news(session, limit=10, hours=72, terms=terms)
    if not items:
        await message.answer("No matching news collected yet. News is refreshed every few minutes.")
        return
    lines = [f"<b>{h(title)}</b>", ""]
    for n in items:
        when = n.published_at.strftime("%d %b %H:%M UTC") if n.published_at else ""
        lines.append(f"• <a href=\"{h(n.url)}\">{h(n.title)}</a>\n  <i>{h(n.source)} {when}</i>")
    await message.answer("\n".join(lines), disable_web_page_preview=True)


# --- research --------------------------------------------------------------


@router.message(Command("research"))
async def cmd_research(
    message: Message,
    command: CommandObject,
    session: AsyncSession,
    user: User,
    cg: CoinGeckoClient,
    assistant: ResearchAssistant,
    settings: Settings,
) -> None:
    question = (command.args or "").strip()
    if not question:
        await message.answer(
            "Ask a question after the command, for example:\n"
            "/research Why is Bitcoin falling today?\n"
            "/research What happened in crypto in the last 24 hours?"
        )
        return
    if not assistant.enabled:
        await message.answer("AI research is not configured on this bot yet.")
        return
    if len(question) > 1000:
        await message.answer("Please keep the question under 1000 characters.")
        return

    now = time.monotonic()
    wait = settings.research_cooldown_seconds - (now - _research_last_call.get(user.telegram_id, -1e9))
    if wait > 0:
        await message.answer(f"Please wait {int(wait) + 1}s before the next research request.")
        return
    _research_last_call[user.telegram_id] = now

    status = await message.answer("🔎 Collecting market data and news, then analysing…")
    if message.bot:
        await message.bot.send_chat_action(message.chat.id, ChatAction.TYPING)
    try:
        evidence = await build_evidence(session, cg, question, settings.tracked_coins_count)
        report = await assistant.answer(question, evidence)
    except ResearchUnavailable as exc:
        await status.edit_text(str(exc))
        return
    except Exception:
        log.exception("research failed")
        await status.edit_text("Research failed unexpectedly. Please try again later.")
        return

    await status.delete()
    for chunk in split_message(report):
        await message.answer(chunk, parse_mode=None, disable_web_page_preview=True)


# --- watchlist -------------------------------------------------------------


async def _render_watchlist(session: AsyncSession, user: User) -> tuple[str, InlineKeyboardMarkup | None]:
    items = await services.get_watchlist(session, user)
    if not items:
        return "Your watchlist is empty. Add a coin with /watchlist add btc", None
    text = "<b>Your watchlist</b>\n" + "\n".join(f"• {h(i.name)} ({h(i.symbol)})" for i in items)
    text += "\n\nYou'll get news that mentions these coins. Tap to remove:"
    kb = InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text=f"✖ {i.symbol}", callback_data=f"wl:rm:{i.coin_id}")] for i in items
        ]
    )
    return text, kb


@router.message(Command("watchlist"))
async def cmd_watchlist(
    message: Message, command: CommandObject, session: AsyncSession, user: User, resolver: CoinResolver
) -> None:
    args = (command.args or "").split()
    if not args:
        text, kb = await _render_watchlist(session, user)
        await message.answer(text, reply_markup=kb)
        return

    action, coins = args[0].lower(), args[1:]
    if action not in ("add", "remove", "rm") or not coins:
        await message.answer("Usage: /watchlist add btc  or  /watchlist remove btc")
        return

    replies = []
    for q in coins[:10]:
        try:
            ref = await resolver.resolve(q)
        except MarketDataError:
            await message.answer("Market data is temporarily unavailable. Please try again shortly.")
            return
        if ref is None:
            replies.append(f"Unknown coin: {h(q)}")
            continue
        if action == "add":
            try:
                added = await services.add_to_watchlist(session, user, ref)
            except services.LimitReached as exc:
                replies.append(str(exc))
                break
            replies.append(f"{'Added' if added else 'Already following'} {h(ref.name)} ({h(ref.symbol)})")
        else:
            removed = await services.remove_from_watchlist(session, user, ref.coin_id)
            replies.append(f"{'Removed' if removed else 'Not on your watchlist:'} {h(ref.name)}")
    await message.answer("\n".join(replies))


@router.callback_query(F.data.startswith("wl:rm:"))
async def cb_watchlist_remove(query: CallbackQuery, session: AsyncSession, user: User) -> None:
    coin_id = (query.data or "").removeprefix("wl:rm:")
    await services.remove_from_watchlist(session, user, coin_id)
    text, kb = await _render_watchlist(session, user)
    if isinstance(query.message, Message):
        await query.message.edit_text(text, reply_markup=kb)
    await query.answer("Removed")


# --- alerts ----------------------------------------------------------------


def _describe_alert(kind: AlertKind, symbol: str, threshold: float) -> str:
    if kind is AlertKind.ABOVE:
        return f"{symbol} above {fmt_price(threshold)}"
    if kind is AlertKind.BELOW:
        return f"{symbol} below {fmt_price(threshold)}"
    return f"{symbol} 24h change ≥ ±{threshold:g}%"


async def _render_alerts(session: AsyncSession, user: User) -> tuple[str, InlineKeyboardMarkup | None]:
    alerts = await services.get_alerts(session, user)
    if not alerts:
        return "No active alerts. Example: /alerts add btc above 120000", None
    text = "<b>Active alerts</b>\n" + "\n".join(
        f"#{a.id} {h(_describe_alert(a.kind, a.symbol, a.threshold))}" for a in alerts
    )
    kb = InlineKeyboardMarkup(
        inline_keyboard=[[InlineKeyboardButton(text=f"✖ #{a.id} {a.symbol}", callback_data=f"al:rm:{a.id}")] for a in alerts]
    )
    return text, kb


@router.message(Command("alerts"))
async def cmd_alerts(
    message: Message, command: CommandObject, session: AsyncSession, user: User, resolver: CoinResolver
) -> None:
    args = (command.args or "").split()
    if not args:
        text, kb = await _render_alerts(session, user)
        await message.answer(text, reply_markup=kb)
        return

    action = args[0].lower()
    if action in ("remove", "rm", "delete") and len(args) == 2 and args[1].lstrip("#").isdigit():
        ok = await services.delete_alert(session, user, int(args[1].lstrip("#")))
        await message.answer("Alert removed." if ok else "Alert not found.")
        return

    usage = "Usage: /alerts add &lt;coin&gt; above|below &lt;price&gt;  or  /alerts add &lt;coin&gt; change &lt;percent&gt;"
    if action != "add" or len(args) != 4:
        await message.answer(usage)
        return
    try:
        kind = AlertKind(args[2].lower())
        threshold = float(args[3].replace(",", "").rstrip("%").lstrip("$"))
    except ValueError:
        await message.answer(usage)
        return

    try:
        ref = await resolver.resolve(args[1])
    except MarketDataError:
        await message.answer("Market data is temporarily unavailable. Please try again shortly.")
        return
    if ref is None:
        await message.answer(f"Unknown coin: {h(args[1])}")
        return
    try:
        alert = await services.add_alert(session, user, ref, kind, threshold)
    except (ValueError, services.LimitReached) as exc:
        await message.answer(h(str(exc)))
        return
    await message.answer(f"✅ Alert #{alert.id} set: {h(_describe_alert(kind, ref.symbol, threshold))}")


@router.callback_query(F.data.startswith("al:rm:"))
async def cb_alert_remove(query: CallbackQuery, session: AsyncSession, user: User) -> None:
    alert_id = (query.data or "").removeprefix("al:rm:")
    if alert_id.isdigit():
        await services.delete_alert(session, user, int(alert_id))
    text, kb = await _render_alerts(session, user)
    if isinstance(query.message, Message):
        await query.message.edit_text(text, reply_markup=kb)
    await query.answer("Removed")
