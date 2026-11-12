"""Handlers for 💼 Portfolio: connecting exchange accounts with read-only API keys."""

from __future__ import annotations

import contextlib
import logging

from aiogram import F, Router
from aiogram.enums import ChatType
from aiogram.exceptions import TelegramAPIError
from aiogram.filters import Command, StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message

from bot import portfolio_ui as ui
from bot.context import App
from bot.formatting import h
from bot.handlers import _ack, _edit, _typing
from bot.portfolio_ui import ConnectFlow, PortfolioCB
from bot.screens import BTN_PORTFOLIO
from collectors.accounts import SUPPORTED, AccountError, parse_credentials

log = logging.getLogger(__name__)
router = Router(name="portfolio")


@router.message(Command("portfolio", "balance", "connect"))
@router.message(F.text == BTN_PORTFOLIO)
async def cmd_portfolio(message: Message, app: App) -> None:
    if message.from_user is None:
        return
    await _typing(message)
    text, kb = await ui.portfolio_screen(app, message.from_user.id)
    await message.answer(text, reply_markup=kb)


@router.message(Command("cancel"))
async def cmd_cancel(message: Message, state: FSMContext) -> None:
    if await state.get_state() is None:
        await message.answer("Nothing to cancel.")
        return
    await state.clear()
    await message.answer("Cancelled. Nothing was saved.")


@router.callback_query(PortfolioCB.filter())
async def cb_portfolio(query: CallbackQuery, callback_data: PortfolioCB, app: App, state: FSMContext) -> None:
    uid, action, ex = query.from_user.id, callback_data.action, callback_data.ex
    if action == "pick" and ex in SUPPORTED:
        chat = query.message.chat if isinstance(query.message, Message) else None
        if chat is None or chat.type != ChatType.PRIVATE:
            await _ack(query, "For safety, connect exchanges in a private chat with the bot.", show_alert=True)
            return
        await state.set_state(ConnectFlow.credentials)
        await state.update_data(exchange=ex)
        await _edit(query, *ui.connect_instructions(ex))
        return
    if action == "cancel":
        await state.clear()
        await _ack(query, "Cancelled")
        await _edit(query, *await ui.portfolio_screen(app, uid), acked=True)
        return
    if action == "add":
        await _edit(query, *await ui.add_screen(app, uid))
        return
    if action == "del":
        await _edit(query, *await ui.disconnect_screen(app, uid, ex))
        return
    if action == "delok":
        await app.db.delete_account(uid, ex)
        ui.forget(uid)
        await _ack(query, f"{ui.ex_title(ex)} disconnected")
        await _edit(query, *await ui.portfolio_screen(app, uid), acked=True)
        return
    await _ack(query, "Loading balances…")
    await _edit(query, *await ui.portfolio_screen(app, uid, refresh=action == "refresh"), acked=True)


@router.message(StateFilter(ConnectFlow.credentials), F.text & ~F.text.startswith("/"))
async def got_credentials(message: Message, app: App, state: FSMContext) -> None:
    if not any(len(p) >= 16 for p in (message.text or "").split()):  # ordinary text, not keys: leave the flow
        await state.clear()
        await message.answer("Exchange connection cancelled. Send /connect to start again, or repeat your message.")
        return
    # the message holds secret keys: remove it from the chat first
    with contextlib.suppress(TelegramAPIError):
        await message.delete()
    if message.from_user is None or app.vault is None or app.accounts is None:
        return
    exchange = (await state.get_data()).get("exchange", "")
    if exchange not in SUPPORTED:
        await state.clear()
        return
    name, needs_password, _ = SUPPORTED[exchange]
    await _typing(message)
    try:
        creds = parse_credentials(message.text or "", needs_password)
        view, perms = await app.accounts.connect(exchange, creds)
    except AccountError as exc:
        await message.answer(f"❌ {exc}\n\nTry again, or send /cancel.")
        return
    await app.db.save_account(message.from_user.id, exchange, app.vault.seal(creds), perms.label)
    await state.clear()
    ui.forget(message.from_user.id)
    log.info("user %s connected %s (%s)", message.from_user.id, exchange, perms.label)
    note = ""
    if perms.trade:
        note = "\n⚠️ This key can also <b>trade</b>. The bot never uses that, but a read-only key is safer."
    await message.answer(
        f"✅ <b>{h(name)} connected</b>: {len(view.holdings)} coins worth ${view.total:,.2f}. "
        f"Your message with the key was deleted.{note}"
    )
    text, kb = await ui.portfolio_screen(app, message.from_user.id)
    await message.answer(text, reply_markup=kb)
