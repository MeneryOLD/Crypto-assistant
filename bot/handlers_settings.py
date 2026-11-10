"""Handlers for /start (first-run setup), Home, Settings, quiet hours and pausing alerts."""

from __future__ import annotations

import logging

from aiogram import F, Router
from aiogram.filters import Command, CommandStart
from aiogram.types import CallbackQuery, Message

from bot import settings_ui as ui
from bot.context import App
from bot.handlers import _ack, _edit, _typing, _user
from bot.screens import BTN_HOME, BTN_SETTINGS, MAIN_MENU, PauseCB
from bot.settings_ui import HomeCB, QuietCB, SettingsCB, SetupCB

log = logging.getLogger(__name__)
router = Router(name="settings")


async def _send_home(message: Message, app: App, user) -> None:  # type: ignore[no-untyped-def]
    text, kb = await ui.home(app, user)
    await message.answer(text, reply_markup=kb)


# --- start & first-run setup --------------------------------------------------------------


@router.message(CommandStart())
async def cmd_start(message: Message, app: App) -> None:
    tg = message.from_user
    if tg is None:
        return
    user, _ = await app.db.upsert_user(tg.id, tg.username, tg.first_name)
    if not user.onboarded:
        await message.answer(ui.WELCOME, reply_markup=MAIN_MENU)  # also installs the menu keyboard
        text, kb = ui.setup_step(user, "tz")
        await message.answer(text, reply_markup=kb)
        return
    await message.answer("👋 Welcome back!", reply_markup=MAIN_MENU)  # refresh the menu keyboard
    await _typing(message)
    await _send_home(message, app, user)


@router.callback_query(SetupCB.filter())
async def cb_setup(query: CallbackQuery, callback_data: SetupCB, app: App) -> None:
    tg = query.from_user
    step, value = callback_data.step, callback_data.value
    user = await _user(app.db, tg)
    if step == "tz" and -720 <= value <= 840:
        user = await app.db.update_user(tg.id, tz_offset=value)
        await _edit(query, *ui.setup_step(user, "alerts"))  # type: ignore[arg-type]
        return
    if step == "alerts":
        if value == 0:
            user = await app.db.update_user(tg.id, alerts_on=False)
        else:
            user = await app.db.update_user(tg.id, alerts_on=True, alert_level=value)
        await _edit(query, *ui.setup_step(user, "digest"))  # type: ignore[arg-type]
        return
    if step == "digest":
        if value < 0:
            user = await app.db.update_user(tg.id, digest_on=False)
        elif value <= 23:
            user = await app.db.update_user(tg.id, digest_on=True, digest_hour=value)
    # "digest" answered or "skip": finish
    user = await app.db.update_user(tg.id, onboarded=True)
    await _edit(query, ui.setup_done(user), None)  # type: ignore[arg-type]
    if isinstance(query.message, Message):
        await _send_home(query.message, app, user)


# --- home ------------------------------------------------------------------------------------


@router.message(Command("home", "menu"))
@router.message(F.text == BTN_HOME)
async def cmd_home(message: Message, app: App) -> None:
    if message.from_user is None:
        return
    await _typing(message)
    await _send_home(message, app, await _user(app.db, message.from_user))


@router.callback_query(HomeCB.filter())
async def cb_home(query: CallbackQuery, app: App) -> None:
    await _ack(query, "Refreshing…")
    text, kb = await ui.home(app, await _user(app.db, query.from_user))
    await _edit(query, text, kb, acked=True)


# --- settings ----------------------------------------------------------------------------------


@router.message(Command("settings"))
@router.message(F.text == BTN_SETTINGS)
async def cmd_settings(message: Message, app: App) -> None:
    if message.from_user is None:
        return
    text, kb = ui.settings_screen(await _user(app.db, message.from_user))  # type: ignore[arg-type]
    await message.answer(text, reply_markup=kb)


@router.callback_query(SettingsCB.filter())
async def cb_settings(query: CallbackQuery, callback_data: SettingsCB, app: App) -> None:
    tg = query.from_user
    user = await _user(app.db, tg)
    if callback_data.action == "reminders":
        user = await app.db.update_user(tg.id, calendar_alerts=not user.calendar_alerts)  # type: ignore[attr-defined]
    elif callback_data.action == "setup":
        await _edit(query, *ui.setup_step(user, "tz"))  # type: ignore[arg-type]
        return
    await _edit(query, *ui.settings_screen(user))  # type: ignore[arg-type]


@router.callback_query(QuietCB.filter())
async def cb_quiet(query: CallbackQuery, callback_data: QuietCB, app: App) -> None:
    tg = query.from_user
    c = callback_data
    user = await _user(app.db, tg)
    if c.critical in (0, 1):
        user = await app.db.update_user(tg.id, quiet_critical=bool(c.critical))
    elif c.start >= -1 and -1 <= c.end <= 23:
        user = await app.db.update_user(tg.id, quiet_start=c.start, quiet_end=c.end)
    await _edit(query, *ui.quiet_screen(user))  # type: ignore[arg-type]


@router.callback_query(PauseCB.filter())
async def cb_pause(query: CallbackQuery, callback_data: PauseCB, app: App) -> None:
    tg = query.from_user
    user = await _user(app.db, tg)
    until = ui.pause_until(user, callback_data.minutes)  # type: ignore[arg-type]
    user = await app.db.update_user(tg.id, muted_until=until)
    if callback_data.back == "alert":
        if until is None:
            await _ack(query, "Alerts resumed")
        else:
            await _ack(query, f"🔕 Alerts paused until {user.local(until):%H:%M}. Resume any time in ⚙️ Settings.", show_alert=True)  # type: ignore[union-attr]
        return
    await _edit(query, *ui.settings_screen(user))  # type: ignore[arg-type]
