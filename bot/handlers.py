"""Telegram handlers: commands, menu buttons, inline buttons and free text all lead to screens."""

from __future__ import annotations

import logging
import re

from aiogram import F, Router
from aiogram.enums import ChatAction
from aiogram.exceptions import TelegramBadRequest
from aiogram.filters import Command, CommandObject, CommandStart
from aiogram.types import CallbackQuery, InlineKeyboardMarkup, Message

from bot import screens
from bot.notifier import Notifier
from bot.screens import (
    BTN_ALERTS,
    BTN_HISTORY,
    BTN_MARKETS,
    BTN_NEWS,
    BTN_STATUS,
    BTN_TOP,
    MAIN_MENU,
    AlertsCB,
    HistoryCB,
    ItemCB,
    MarketCB,
    NewsCB,
    StatusCB,
)
from collectors.exchanges import ExchangeHub
from collectors.fear_greed import FearGreedClient
from collectors.news import CATEGORIES, NewsCollector
from storage.db import Database

log = logging.getLogger(__name__)
router = Router()

COIN_RE = re.compile(r"^[A-Za-z0-9]{2,10}$")


async def _typing(message: Message) -> None:
    if message.bot:
        await message.bot.send_chat_action(message.chat.id, ChatAction.TYPING)


async def _ack(query: CallbackQuery, text: str | None = None, show_alert: bool = False) -> None:
    """Confirm a button press. Telegram expires presses after ~15 s (e.g. after a network hiccup) — ignore that."""
    try:
        await query.answer(text, show_alert=show_alert)
    except TelegramBadRequest as exc:
        if "query is too old" not in str(exc) and "query ID is invalid" not in str(exc):
            raise


async def _edit(query: CallbackQuery, text: str, kb: InlineKeyboardMarkup | None, *, acked: bool = False) -> None:
    """Update the screen in place (no new messages piling up)."""
    if isinstance(query.message, Message):
        try:
            await query.message.edit_text(text, reply_markup=kb)
        except TelegramBadRequest as exc:
            if "not modified" not in str(exc):
                raise
            if not acked:
                await _ack(query, "Already up to date")
            return
    if not acked:
        await _ack(query)


# --- start / help ------------------------------------------------------------


@router.message(CommandStart())
async def cmd_start(
    message: Message, db: Database, hub: ExchangeHub, collector: NewsCollector, fg: FearGreedClient
) -> None:
    tg = message.from_user
    user = None
    if tg:
        user, created = await db.upsert_user(tg.id, tg.username, tg.first_name)
    await _typing(message)
    text = await screens.home(db, hub, collector, fg, tg.first_name if tg else None, user)
    await message.answer(text, reply_markup=MAIN_MENU)


@router.message(Command("help"))
async def cmd_help(message: Message) -> None:
    await message.answer(screens.HELP, reply_markup=MAIN_MENU)


# --- news --------------------------------------------------------------------


@router.message(Command("news"))
@router.message(F.text == BTN_NEWS)
async def cmd_news(message: Message, db: Database, command: CommandObject | None = None) -> None:
    arg = (command.args or "").strip() if command else ""
    if arg.lower() in CATEGORIES:
        text, kb = await screens.news(db, cat=arg.lower())
    else:
        text, kb = await screens.news(db, q=screens.safe_query(arg))
    await message.answer(text, reply_markup=kb)


@router.callback_query(NewsCB.filter())
async def cb_news(query: CallbackQuery, callback_data: NewsCB, db: Database) -> None:
    c = callback_data
    await _edit(query, *await screens.news(db, c.cat, c.page, c.q, c.s))


@router.message(Command("top"))
@router.message(F.text == BTN_TOP)
async def cmd_top(message: Message, db: Database) -> None:
    text, kb = await screens.news(db, s="top")
    await message.answer(text, reply_markup=kb)


@router.callback_query(ItemCB.filter())
async def cb_item(query: CallbackQuery, callback_data: ItemCB, db: Database) -> None:
    c = callback_data
    text, kb = await screens.article(db, c.id, c.cat, c.page, c.q, c.s)
    if c.new and isinstance(query.message, Message):
        # Opened from an alert: keep the alert, show details as a new message.
        await query.message.answer(text, reply_markup=kb)
        await _ack(query)
    else:
        await _edit(query, text, kb)


# --- alerts ------------------------------------------------------------------


async def _user(db: Database, tg_user) -> object:  # type: ignore[no-untyped-def]
    user = await db.get_user(tg_user.id)
    if user is None:
        user, _ = await db.upsert_user(tg_user.id, tg_user.username, tg_user.first_name)
    return user


@router.message(Command("alerts"))
@router.message(F.text == BTN_ALERTS)
async def cmd_alerts(message: Message, db: Database) -> None:
    if message.from_user is None:
        return
    user = await _user(db, message.from_user)
    text, kb = await screens.alerts_settings(db, user)  # type: ignore[arg-type]
    await message.answer(text, reply_markup=kb)


@router.callback_query(AlertsCB.filter())
async def cb_alerts(query: CallbackQuery, callback_data: AlertsCB, db: Database, notifier: Notifier) -> None:
    tg = query.from_user
    user = await _user(db, tg)
    action, value = callback_data.action, callback_data.value
    if action == "on":
        user = await db.update_user(tg.id, alerts_on=True)
    elif action == "off":
        user = await db.update_user(tg.id, alerts_on=False)
    elif action == "level" and value.isdigit():
        user = await db.update_user(tg.id, alert_level=int(value), alerts_on=True)
    elif action == "cat" and value in CATEGORIES:
        cats = screens.toggle_category(user.categories, value)  # type: ignore[attr-defined]
        user = await db.update_user(tg.id, alert_categories=",".join(sorted(cats)))
    elif action == "test":
        top = screens.dedupe_stories(await db.top_news(hours=24, limit=5))
        if not top:
            await _ack(query, "No scored news yet — try again in a minute.", show_alert=True)
            return
        await notifier.send(user, top[0], test=True)  # type: ignore[arg-type]
        await _ack(query, "Test alert sent 👇")
        return
    await _edit(query, *await screens.alerts_settings(db, user))  # type: ignore[arg-type]


# --- markets -----------------------------------------------------------------


@router.message(Command("markets", "ticker", "price"))
@router.message(F.text == BTN_MARKETS)
async def cmd_markets(
    message: Message, hub: ExchangeHub, fg: FearGreedClient, command: CommandObject | None = None
) -> None:
    coin = ((command.args or "").split() or ["BTC"])[0] if command else "BTC"
    await _typing(message)
    text, kb = await screens.market(hub, coin, fg)
    await message.answer(text, reply_markup=kb)


@router.message(Command("book"))
async def cmd_book(message: Message, command: CommandObject, hub: ExchangeHub) -> None:
    args = (command.args or "").split()
    coin = args[0] if args else "BTC"
    exchange = args[1] if len(args) > 1 else hub.names[0]
    await _typing(message)
    text, kb = await screens.order_book(hub, coin, exchange)
    await message.answer(text, reply_markup=kb)


@router.message(Command("arb"))
async def cmd_arb(message: Message, command: CommandObject, hub: ExchangeHub) -> None:
    coin = ((command.args or "").split() or ["BTC"])[0]
    await _typing(message)
    text, kb = await screens.buy_sell_gap(hub, coin)
    await message.answer(text, reply_markup=kb)


@router.callback_query(MarketCB.filter())
async def cb_market(query: CallbackQuery, callback_data: MarketCB, hub: ExchangeHub, fg: FearGreedClient) -> None:
    await _ack(query, "Loading…")  # exchanges can take a few seconds; confirm the tap right away
    if callback_data.view == "book":
        screen = await screens.order_book(hub, callback_data.coin, callback_data.ex)
    elif callback_data.view == "arb":
        screen = await screens.buy_sell_gap(hub, callback_data.coin)
    else:
        screen = await screens.market(hub, callback_data.coin, fg)
    await _edit(query, *screen, acked=True)


# --- history -----------------------------------------------------------------


@router.message(Command("history"))
@router.message(F.text == BTN_HISTORY)
async def cmd_history(message: Message, db: Database, command: CommandObject | None = None) -> None:
    arg = (command.args or "").strip().lower() if command else ""
    text, kb = await screens.history(db, cat=arg if arg in CATEGORIES else "all")
    await message.answer(text, reply_markup=kb)


@router.callback_query(HistoryCB.filter())
async def cb_history(query: CallbackQuery, callback_data: HistoryCB, db: Database) -> None:
    await _edit(query, *await screens.history(db, callback_data.cat, callback_data.hz, callback_data.mode))


# --- status ------------------------------------------------------------------


@router.message(Command("status", "sources", "exchanges"))
@router.message(F.text == BTN_STATUS)
async def cmd_status(message: Message, db: Database, hub: ExchangeHub, collector: NewsCollector) -> None:
    await _typing(message)
    text, kb = await screens.status(db, hub, collector)
    await message.answer(text, reply_markup=kb)


@router.callback_query(StatusCB.filter())
async def cb_status(
    query: CallbackQuery, callback_data: StatusCB, db: Database, hub: ExchangeHub, collector: NewsCollector
) -> None:
    await _ack(query, "Checking…")
    await _edit(query, *await screens.status(db, hub, collector, callback_data.full), acked=True)


# --- free text: a coin or a news search ------------------------------------------


@router.message(F.text & ~F.text.startswith("/"))
async def free_text(message: Message, db: Database, hub: ExchangeHub, fg: FearGreedClient) -> None:
    text = (message.text or "").strip()
    await _typing(message)
    if COIN_RE.match(text) and await hub.is_listed(text):
        screen = await screens.market(hub, text, fg)
    else:
        screen = await screens.news(db, q=screens.safe_query(text))
    await message.answer(screen[0], reply_markup=screen[1])
