"""💼 Portfolio: the user's own exchange accounts (read-only API keys), valued in USD."""

from __future__ import annotations

import asyncio
import time

from aiogram.filters.callback_data import CallbackData
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from bot.context import App
from bot.formatting import h
from bot.screens import Screen
from collectors.accounts import SUPPORTED, AccountView, Portfolio
from storage.vault import VaultError

CACHE_SECONDS = 60
SHOW_HOLDINGS = 12
_cache: dict[int, tuple[float, Portfolio]] = {}


class PortfolioCB(CallbackData, prefix="pf"):
    action: str = ""  # "" show · refresh · add · pick · del · delok · cancel
    ex: str = ""


class ConnectFlow(StatesGroup):
    credentials = State()


def forget(user_id: int) -> None:
    _cache.pop(user_id, None)


def _usd(v: float) -> str:
    return f"${v:,.2f}" if abs(v) < 10_000 else f"${v:,.0f}"


def _signed_usd(v: float) -> str:
    return ("+" if v >= 0 else "−") + _usd(abs(v))


def _pct(v: float | None) -> str:
    return "" if v is None else f"{v:+.1f}%".replace("-", "−")


def _amount(v: float) -> str:
    if v >= 1000:
        return f"{v:,.0f}"
    if v >= 1:
        return f"{v:,.4f}".rstrip("0").rstrip(".")
    return f"{v:.8f}".rstrip("0").rstrip(".")


def ex_title(exchange: str) -> str:
    return SUPPORTED.get(exchange, (exchange.title(),))[0]


def _exchange_buttons(action: str, exchanges: list[str]) -> list[list[InlineKeyboardButton]]:
    buttons = [InlineKeyboardButton(text=ex_title(e), callback_data=PortfolioCB(action=action, ex=e).pack()) for e in exchanges]
    return [buttons[i : i + 3] for i in range(0, len(buttons), 3)]


async def load_portfolio(app: App, user_id: int, *, refresh: bool = False) -> Portfolio | None:
    cached = _cache.get(user_id)
    if cached and not refresh and time.monotonic() - cached[0] < CACHE_SECONDS:
        return cached[1]
    accounts = await app.db.accounts(user_id)
    if not accounts or app.vault is None or app.accounts is None:
        return None

    async def one(acc) -> AccountView:  # type: ignore[no-untyped-def]
        try:
            creds = app.vault.unseal(acc.credentials)  # type: ignore[union-attr]
        except VaultError as exc:
            return AccountView(acc.exchange, error=str(exc))
        return await app.accounts.read(acc.exchange, creds)  # type: ignore[union-attr]

    portfolio = Portfolio(list(await asyncio.gather(*(one(a) for a in accounts))))
    _cache[user_id] = (time.monotonic(), portfolio)
    return portfolio


def intro_screen() -> Screen:
    text = "\n".join([
        "💼 <b>Connect your exchange</b>",
        "",
        "See all your balances in one place: total value in USD, each coin, 24 h profit or loss, across exchanges.",
        "",
        "🔒 <b>Read-only and safe</b>",
        "• You create an API key with <b>Read</b> permission only: the bot can see balances, nothing else",
        "• Keys that can withdraw funds are refused",
        "• Keys are stored encrypted; you can disconnect any time (and delete the key on the exchange)",
        "",
        "Pick your exchange:",
    ])
    rows = _exchange_buttons("pick", list(SUPPORTED))
    return text, InlineKeyboardMarkup(inline_keyboard=rows)


def connect_instructions(exchange: str) -> Screen:
    name, needs_password, url = SUPPORTED[exchange]
    send = "<code>API_KEY SECRET PASSPHRASE</code>" if needs_password else "<code>API_KEY SECRET</code>"
    lines = [
        f"🔌 <b>Connect {h(name)}</b>",
        "",
        f'1. Open <a href="{url}">{h(name)} → API management</a>',
        "2. Create a new API key with <b>Read</b> permission only. Leave trading and withdrawals <b>off</b>",
    ]
    if needs_password:
        lines.append("   (you set a passphrase while creating it; you'll need it below)")
    lines += [
        "3. No IP restriction (or add the IP of the server the bot runs on)",
        f"4. Send them here in one message: {send}",
        "",
        "🔒 I delete your message right away and keep the key encrypted.",
        "Send /cancel to stop.",
    ]
    kb = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="✖ Cancel", callback_data=PortfolioCB(action="cancel").pack())]])
    return "\n".join(lines), kb


async def portfolio_screen(app: App, user_id: int, *, refresh: bool = False) -> Screen:
    portfolio = await load_portfolio(app, user_id, refresh=refresh)
    if portfolio is None:
        return intro_screen()

    lines = [f"💼 <b>Your portfolio</b> · <b>{_usd(portfolio.total)}</b>"]
    if (pct := portfolio.change_pct) is not None and portfolio.total > 0:
        icon = "🟢" if portfolio.change_usd >= 0 else "🔴"
        lines.append(f"{icon} 24h: {_signed_usd(portfolio.change_usd)} ({_pct(pct)})")

    holdings = portfolio.by_asset()
    if holdings:
        lines += ["", "<b>Holdings</b>"]
        for i, hd in enumerate(holdings[:SHOW_HOLDINGS], 1):
            share = (hd.usd or 0) / portfolio.total * 100 if portfolio.total else 0
            change = f" · {_pct(hd.change_pct)}" if hd.change_pct else ""
            lines.append(f"{i}. <b>{h(hd.asset)}</b> {_amount(hd.amount)} · {_usd(hd.usd or 0)} ({share:.0f}%){change}")
        if len(holdings) > SHOW_HOLDINGS:
            rest = sum(x.usd or 0 for x in holdings[SHOW_HOLDINGS:])
            lines.append(f"<i>+ {len(holdings) - SHOW_HOLDINGS} more · {_usd(rest)}</i>")

    lines += ["", "<b>By exchange</b>"]
    for a in portfolio.accounts:
        if a.error:
            lines.append(f"⚠️ {h(ex_title(a.exchange))}: {h(a.error)}")
            continue
        extra = []
        if a.dust:
            extra.append(f"{a.dust} tiny")
        if a.unpriced:
            extra.append("no USD price: " + ", ".join(h(x) for x in a.unpriced[:5]))
        tail = f" <i>({'; '.join(extra)})</i>" if extra else ""
        lines.append(f"• {h(ex_title(a.exchange))}: <b>{_usd(a.total)}</b> · {len(a.holdings)} coins{tail}")
    if not holdings and not any(a.error for a in portfolio.accounts):
        lines += ["", "<i>No balances above $1 yet.</i>"]
    lines += ["", "<i>Spot and funding wallets, prices from each exchange. Read-only: the bot can't trade or withdraw.</i>"]

    kb = InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="🔄 Refresh", callback_data=PortfolioCB(action="refresh").pack()),
            InlineKeyboardButton(text="➕ Add exchange", callback_data=PortfolioCB(action="add").pack()),
        ],
        [InlineKeyboardButton(text="🔌 Disconnect…", callback_data=PortfolioCB(action="del").pack())],
    ])
    return "\n".join(lines), kb


async def add_screen(app: App, user_id: int) -> Screen:
    connected = {a.exchange for a in await app.db.accounts(user_id)}
    rows = _exchange_buttons("pick", [e for e in SUPPORTED if e not in connected])
    rows.append([InlineKeyboardButton(text="⬅️ Back", callback_data=PortfolioCB().pack())])
    note = f"\nConnected: {', '.join(ex_title(e) for e in sorted(connected))}" if connected else ""
    return f"➕ <b>Add an exchange</b>{note}\n\nPick one:", InlineKeyboardMarkup(inline_keyboard=rows)


async def disconnect_screen(app: App, user_id: int, exchange: str = "") -> Screen:
    back = [InlineKeyboardButton(text="⬅️ Back", callback_data=PortfolioCB().pack())]
    if exchange:
        text = (f"🔌 Disconnect <b>{h(ex_title(exchange))}</b>? Its key is deleted from the bot.\n"
                "<i>Also delete the key on the exchange if you won't use it again.</i>")
        yes = InlineKeyboardButton(text="Yes, disconnect", callback_data=PortfolioCB(action="delok", ex=exchange).pack())
        return text, InlineKeyboardMarkup(inline_keyboard=[[yes], back])
    rows = _exchange_buttons("del", [a.exchange for a in await app.db.accounts(user_id)])
    return "🔌 <b>Which exchange should I disconnect?</b>", InlineKeyboardMarkup(inline_keyboard=[*rows, back])
