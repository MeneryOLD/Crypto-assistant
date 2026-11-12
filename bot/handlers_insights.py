"""Handlers for sentiment, calendar, digest, similar past events and admin."""

from __future__ import annotations

import logging

from aiogram import F, Router
from aiogram.filters import Command, CommandObject
from aiogram.types import CallbackQuery, Message

from bot import insights
from bot.context import App
from bot.forecast_ui import ForecastCB, TechCB, forecast_screen, technicals_screen
from bot.handlers import _ack, _edit, _typing, _user
from bot.insights import AdminCB, CalendarCB, DigestCB, SentimentCB
from bot.jobs import DigestJob
from bot.screens import BTN_CALENDAR, BTN_DIGEST, BTN_FORECAST, BTN_SENTIMENT, SimilarCB

log = logging.getLogger(__name__)
router = Router(name="insights")


# --- sentiment -----------------------------------------------------------------


@router.message(Command("sentiment", "mood"))
@router.message(F.text == BTN_SENTIMENT)
async def cmd_sentiment(message: Message, app: App) -> None:
    await _typing(message)
    text, kb = await insights.sentiment(app)
    await message.answer(text, reply_markup=kb)


@router.callback_query(SentimentCB.filter())
async def cb_sentiment(query: CallbackQuery, callback_data: SentimentCB, app: App) -> None:
    await _ack(query, "Loading…")
    text, kb = await insights.sentiment(app)
    if callback_data.refresh:
        await _edit(query, text, kb, acked=True)
    elif isinstance(query.message, Message):  # opened from the digest: keep the digest, answer below it
        await query.message.answer(text, reply_markup=kb)


# --- calendar ------------------------------------------------------------------------


@router.message(Command("calendar"))
@router.message(F.text == BTN_CALENDAR)
async def cmd_calendar(message: Message, app: App) -> None:
    if message.from_user is None:
        return
    user = await _user(app.db, message.from_user)
    text, kb = await insights.calendar(app, user)  # type: ignore[arg-type]
    await message.answer(text, reply_markup=kb)


@router.callback_query(CalendarCB.filter())
async def cb_calendar(query: CallbackQuery, callback_data: CalendarCB, app: App) -> None:
    user = await _user(app.db, query.from_user)
    if callback_data.toggle:
        user = await app.db.update_user(query.from_user.id, calendar_alerts=not user.calendar_alerts)  # type: ignore[attr-defined]
    await _edit(query, *await insights.calendar(app, user, callback_data.week, callback_data.imp))  # type: ignore[arg-type]


# --- digest ----------------------------------------------------------------------------


@router.message(Command("digest"))
@router.message(F.text == BTN_DIGEST)
async def cmd_digest(message: Message, app: App) -> None:
    if message.from_user is None:
        return
    user = await _user(app.db, message.from_user)
    text, kb = await insights.digest_settings(app, user)  # type: ignore[arg-type]
    await message.answer(text, reply_markup=kb)


@router.callback_query(DigestCB.filter())
async def cb_digest(query: CallbackQuery, callback_data: DigestCB, app: App) -> None:
    tg = query.from_user
    action, value = callback_data.action, callback_data.value
    user = await _user(app.db, tg)
    if action == "on":
        user = await app.db.update_user(tg.id, digest_on=True)
    elif action == "off":
        user = await app.db.update_user(tg.id, digest_on=False)
    elif action == "hour" and 0 <= value <= 23:
        user = await app.db.update_user(tg.id, digest_hour=value, digest_on=True)
    elif action == "tz" and -720 <= value <= 840:
        user = await app.db.update_user(tg.id, tz_offset=value)
    elif action == "send":
        await _ack(query, "Preparing your digest…")
        if query.bot:
            await DigestJob(app, query.bot).send(user)
        return
    await _edit(query, *await insights.digest_settings(app, user))  # type: ignore[arg-type]


# --- similar past events -------------------------------------------------------------


@router.message(Command("similar"))
async def cmd_similar(message: Message, command: CommandObject, app: App) -> None:
    text = (command.args or "").strip()
    if len(text) < 4:
        await message.answer(
            "Describe an event and I'll show what BTC did after similar news.\n"
            "Examples:\n<code>/similar fed rate hike</code>\n<code>/similar exchange hack</code>\n"
            "<code>/similar trump tariffs china</code>\n<code>/similar spot ETF approval</code>"
        )
        return
    await _typing(message)
    reply, kb = await insights.similar_for_text(app, text[:200])
    await message.answer(reply, reply_markup=kb)


@router.callback_query(SimilarCB.filter())
async def cb_similar(query: CallbackQuery, callback_data: SimilarCB, app: App) -> None:
    await _ack(query, "Searching history…")
    text, kb = await insights.similar_for_item(app, callback_data.id)
    if callback_data.new and isinstance(query.message, Message):
        await query.message.answer(text, reply_markup=kb)
    else:
        await _edit(query, text, kb, acked=True)


# --- admin -------------------------------------------------------------------------------


@router.message(Command("admin"))
async def cmd_admin(message: Message, app: App) -> None:
    if message.from_user is None or not app.is_admin(message.from_user.id):
        await message.answer("This command is for the bot's administrators.")
        return
    text, kb = await insights.admin(app)
    await message.answer(text, reply_markup=kb)


@router.callback_query(AdminCB.filter())
async def cb_admin(query: CallbackQuery, app: App) -> None:
    if not app.is_admin(query.from_user.id):
        await _ack(query, "Admins only", show_alert=True)
        return
    await _edit(query, *await insights.admin(app))


# --- forecast --------------------------------------------------------------------------------


@router.message(Command("forecast", "predict"))
@router.message(F.text == BTN_FORECAST)
async def cmd_forecast(message: Message, app: App) -> None:
    await _typing(message)
    text, kb = await forecast_screen(app)
    await message.answer(text, reply_markup=kb)


@router.callback_query(ForecastCB.filter())
async def cb_forecast(query: CallbackQuery, callback_data: ForecastCB, app: App) -> None:
    await _ack(query, "Updating the forecast…" if callback_data.refresh else None)
    if callback_data.refresh and app.forecast:
        await app.forecast.run()  # cached for 15 minutes
    text, kb = await forecast_screen(app)
    await _edit(query, text, kb, acked=True)


@router.message(Command("ta", "technicals", "indicators"))
async def cmd_technicals(message: Message, command: CommandObject, app: App) -> None:
    coin = ((command.args or "").split() or ["BTC"])[0]
    await _typing(message)
    text, kb = await technicals_screen(app, coin)
    await message.answer(text, reply_markup=kb)


@router.callback_query(TechCB.filter())
async def cb_technicals(query: CallbackQuery, callback_data: TechCB, app: App) -> None:
    await _ack(query, "Calculating indicators…")
    text, kb = await technicals_screen(app, callback_data.coin)
    if isinstance(query.message, Message) and query.message.text and "technical analysis" not in query.message.text:
        await query.message.answer(text, reply_markup=kb)  # opened from Markets / Forecast: keep that screen
    else:
        await _edit(query, text, kb, acked=True)
