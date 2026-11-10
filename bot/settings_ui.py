"""Convenience screens: first-run setup, one Settings screen, quiet hours, pausing alerts, and the Home dashboard."""

from __future__ import annotations

import asyncio
from datetime import timedelta

from aiogram.filters.callback_data import CallbackData
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from bot.context import App
from bot.formatting import CATEGORY_ICONS, ago, fmt_pct, fmt_price, h
from bot.insights import CalendarCB, DigestCB, SentimentCB
from bot.screens import (
    ALERT_LEVELS,
    NEWS_TABS,
    AlertsCB,
    ItemCB,
    MarketCB,
    NewsCB,
    PauseCB,
    Screen,
    SettingsCB,
    StatusCB,
    _rows,
    _source_icon,
    _tab,
    badge,
    dedupe_stories,
)
from collectors.calendar import FLAGS
from collectors.exchanges import Ticker
from storage.db import User, utcnow

# Offsets offered in the setup (minutes from UTC). The user taps the time that matches their clock.
TZ_CHOICES = [-480, -420, -360, -300, -240, -180, 0, 60, 120, 180, 240, 300, 330, 360, 420, 480, 540, 600]
QUIET_CHOICES = [(-1, -1), (22, 7), (23, 7), (0, 8), (1, 9)]


class SetupCB(CallbackData, prefix="su"):
    step: str  # tz | alerts | digest | skip
    value: int = 0


class QuietCB(CallbackData, prefix="qt"):
    start: int = -2  # -2 = just show the screen
    end: int = -1
    critical: int = -1  # 0/1 = set "critical still allowed"


class HomeCB(CallbackData, prefix="hm"):
    pass


def _tz_label(minutes: int) -> str:
    sign = "+" if minutes >= 0 else "−"
    hours, mins = divmod(abs(minutes), 60)
    return f"UTC{sign}{hours}" + (f":{mins:02d}" if mins else "")


def level_name(level: int) -> str:
    return next((name for lvl, name, _ in ALERT_LEVELS if lvl == level), f"{level}+")


def alerts_summary(user: User) -> str:
    """One line: are alerts on, at which level, paused or quiet right now."""
    now = utcnow()
    if not user.alerts_on:
        return "🔕 Alerts off"
    text = f"🔔 Alerts: {level_name(user.alert_level)}"
    if user.categories:
        text += " · " + " ".join(CATEGORY_ICONS.get(c, c) for c in sorted(user.categories))
    if user.is_muted(now):
        text += f" · ⏸ paused until {user.local(user.muted_until):%H:%M}"  # type: ignore[arg-type]
    elif user.is_quiet(now):
        text += " · 🌙 quiet hours now"
    return text


def quiet_summary(user: User) -> str:
    if user.quiet_start < 0:
        return "off"
    crit = "🔴 critical still come in" if user.quiet_critical else "nothing comes in"
    return f"{user.quiet_start:02d}:00–{user.quiet_end:02d}:00 ({crit})"


# --- first-run setup -------------------------------------------------------------

WELCOME = (
    "👋 <b>Welcome to Vexora</b> — your crypto &amp; world news radar.\n\n"
    "I watch 114 sources around the clock, rate every story 1–10, tell you within seconds when something "
    "important happens, and show how Bitcoin reacted to similar news in the past.\n\n"
    "Three quick questions and you're set (you can change everything later in ⚙️ Settings)."
)


def setup_step(user: User, step: str) -> Screen:
    now = utcnow()
    skip = InlineKeyboardButton(text="Skip setup — use defaults", callback_data=SetupCB(step="skip").pack())
    if step == "tz":
        text = (
            "🧭 <b>Step 1 of 3 · Your time</b>\n\n"
            "Which time does your clock show right now? Tap it — that sets your time zone for the digest, "
            "the economic calendar and quiet hours."
        )
        buttons = [
            InlineKeyboardButton(
                text=f"{(now + timedelta(minutes=m)):%H:%M} · {_tz_label(m)}",
                callback_data=SetupCB(step="tz", value=m).pack(),
            )
            for m in TZ_CHOICES
        ]
        return text, InlineKeyboardMarkup(inline_keyboard=[*_rows(buttons, 3), [skip]])
    if step == "alerts":
        text = (
            "🔔 <b>Step 2 of 3 · Alerts</b>\n\n"
            "When something important happens I message you right away. How much do you want to hear?\n\n"
            "🔴 <b>Critical only</b> — ~5 a day: rate decisions, wars, exchange collapses, ETF decisions\n"
            "🟠 <b>Important</b> — ~25 a day: also CPI/jobs data, regulators, big hacks, sanctions\n"
            "🟡 <b>Notable</b> — ~100+ a day: also big market moves and notable politics\n"
            "🔕 <b>No alerts</b> — I'll only answer when you ask"
        )
        rows = [
            [InlineKeyboardButton(text="🔴 Critical only", callback_data=SetupCB(step="alerts", value=9).pack()),
             InlineKeyboardButton(text="🟠 Important", callback_data=SetupCB(step="alerts", value=7).pack())],
            [InlineKeyboardButton(text="🟡 Notable", callback_data=SetupCB(step="alerts", value=5).pack()),
             InlineKeyboardButton(text="🔕 No alerts", callback_data=SetupCB(step="alerts", value=0).pack())],
            [skip],
        ]
        return text, InlineKeyboardMarkup(inline_keyboard=rows)
    # digest
    local = user.local(now)
    text = (
        "☀️ <b>Step 3 of 3 · Morning brief</b>\n\n"
        "Every day I can send a short brief: market mood, prices, today's economic releases and the "
        f"10 most important stories. When? <i>(your time now: {local:%H:%M})</i>"
    )
    hours = [InlineKeyboardButton(text=f"{hr:02d}:00", callback_data=SetupCB(step="digest", value=hr).pack()) for hr in (6, 7, 8, 9, 10, 12)]
    rows = [*_rows(hours, 3), [InlineKeyboardButton(text="🚫 No brief", callback_data=SetupCB(step="digest", value=-1).pack())]]
    return text, InlineKeyboardMarkup(inline_keyboard=rows)


def setup_done(user: User) -> str:
    digest = f"every day at {user.digest_hour:02d}:00" if user.digest_on else "off"
    return (
        "✅ <b>All set!</b>\n\n"
        f"🌍 Time zone: <b>{user.tz_label}</b>\n"
        f"{alerts_summary(user)}\n"
        f"☀️ Morning brief: <b>{digest}</b>\n\n"
        "Tip: tap 🔕 on any alert to pause for an hour or until morning, or set 🌙 quiet hours in ⚙️ Settings."
    )


# --- settings ----------------------------------------------------------------------


def settings_screen(user: User) -> Screen:
    now = utcnow()
    digest = f"{user.digest_hour:02d}:00 every day" if user.digest_on else "off"
    lines = [
        "⚙️ <b>Settings</b>",
        "",
        f"🌍 Time zone: <b>{user.tz_label}</b> (your time {user.local(now):%H:%M})",
        alerts_summary(user),
        f"🌙 Quiet hours: <b>{quiet_summary(user)}</b>",
        f"☀️ Morning brief: <b>{digest}</b>",
        f"⏰ Reminders before big releases: <b>{'on' if user.calendar_alerts else 'off'}</b>",
    ]
    if user.categories:
        lines.append("Alert categories: " + ", ".join(dict(NEWS_TABS)[c] for c in sorted(user.categories)))
    pause_row = (
        [InlineKeyboardButton(text="▶️ Resume alerts", callback_data=PauseCB(minutes=0).pack())]
        if user.is_muted(now)
        else [
            InlineKeyboardButton(text="🔕 Pause 1 h", callback_data=PauseCB(minutes=60).pack()),
            InlineKeyboardButton(text="🔕 Until morning", callback_data=PauseCB(minutes=-1).pack()),
        ]
    )
    kb = [
        [InlineKeyboardButton(text="🔔 Alerts", callback_data=AlertsCB().pack()),
         InlineKeyboardButton(text="☀️ Brief & time zone", callback_data=DigestCB().pack())],
        [InlineKeyboardButton(text="🌙 Quiet hours", callback_data=QuietCB().pack()),
         InlineKeyboardButton(text=f"⏰ Reminders: {'on' if user.calendar_alerts else 'off'}",
                              callback_data=SettingsCB(action="reminders").pack())],
        pause_row,
        [InlineKeyboardButton(text="🧭 Run setup again", callback_data=SettingsCB(action="setup").pack()),
         InlineKeyboardButton(text="📡 System status", callback_data=StatusCB().pack())],
    ]
    return "\n".join(lines), InlineKeyboardMarkup(inline_keyboard=kb)


def quiet_screen(user: User) -> Screen:
    lines = [
        "🌙 <b>Quiet hours</b>",
        "",
        f"Now: <b>{quiet_summary(user)}</b> · times in {user.tz_label}",
        "",
        "During quiet hours alerts and release reminders wait until morning. "
        "You can still let 🔴 critical news through (a war, a rate decision, an exchange collapse).",
    ]
    choices = [
        InlineKeyboardButton(
            text=_tab("Off" if s < 0 else f"{s:02d}–{e:02d}", (s, e) == (user.quiet_start, user.quiet_end) or (s < 0 and user.quiet_start < 0)),
            callback_data=QuietCB(start=s, end=e).pack(),
        )
        for s, e in QUIET_CHOICES
    ]
    crit = InlineKeyboardButton(
        text=("✅ " if user.quiet_critical else "⬜ ") + "Let 🔴 critical news through",
        callback_data=QuietCB(critical=0 if user.quiet_critical else 1).pack(),
    )
    back = InlineKeyboardButton(text="◀️ Settings", callback_data=SettingsCB().pack())
    return "\n".join(lines), InlineKeyboardMarkup(inline_keyboard=[choices, [crit], [back]])


def pause_until(user: User, minutes: int):  # type: ignore[no-untyped-def]
    now = utcnow()
    if minutes == 0:
        return None
    if minutes < 0:  # until morning = the user's brief hour (or 08:00)
        return user.next_local_hour(now, user.digest_hour if user.digest_on else 8)
    return now + timedelta(minutes=minutes)


# --- home dashboard ----------------------------------------------------------------


async def home(app: App, user: User | None, first_name: str | None = None) -> Screen:
    now = utcnow()
    ex = app.hub.names[0]
    btc, eth, fg, snaps, top, upcoming = await asyncio.gather(
        app.hub.ticker(ex, "BTC"),
        app.hub.ticker(ex, "ETH"),
        app.fg.get(),
        app.db.snapshots(days=1),
        app.db.top_news(hours=6, limit=60),
        app.db.calendar_between(now, now + timedelta(days=7), min_impact=3),
        return_exceptions=True,
    )
    local = user.local(now) if user else now
    name = first_name or (user.first_name if user else None)
    lines = [f"🏠 <b>Vexora</b>{' · ' + h(name) if name else ''} · {local:%a %d %b, %H:%M}" + (f" ({user.tz_label})" if user else " UTC"), ""]

    prices = [f"{icon} {coin} <b>${fmt_price(t.last)}</b> {fmt_pct(t.change_pct)}"
              for coin, icon, t in (("BTC", "₿", btc), ("ETH", "Ξ", eth)) if isinstance(t, Ticker)]
    lines.append(" · ".join(prices) if prices else "<i>Prices unavailable right now</i>")
    mood_bits = []
    if isinstance(snaps, list) and snaps and snaps[-1].mood is not None:
        mood_bits.append(f"🌡 Mood <b>{snaps[-1].mood:.0f}</b>/100")
    if fg and not isinstance(fg, BaseException):
        mood_bits.append(f"{fg.emoji} Fear &amp; Greed <b>{fg.value}</b> {h(fg.label)}")
    if mood_bits:
        lines.append(" · ".join(mood_bits))
    if user:
        lines.append(alerts_summary(user))

    if isinstance(upcoming, list) and (us := [e for e in upcoming if e.country in ("USD", "ALL")] or upcoming):
        e = us[0]
        delta = e.starts_at - now
        when = f"in {delta.seconds // 3600}h {delta.seconds % 3600 // 60:02d}m" if delta.days == 0 else f"{(user.local(e.starts_at) if user else e.starts_at):%a %H:%M}"
        lines.append(f"📅 Next big release: {FLAGS.get(e.country, e.country)} <b>{h(e.title)}</b> · {when}")

    stories = dedupe_stories(top)[:3] if isinstance(top, list) else []
    if stories:
        lines += ["", "🔥 <b>Most important right now</b>"]
        lines += [f"{badge(n)} {_source_icon(n.source)}<a href=\"{h(n.url)}\">{h(n.title)}</a> <i>· {ago(n.event_at)}</i>" for n in stories]
    lines += ["", "<i>Type a coin (<code>sol</code>) or any word (<code>fed</code>) to search.</i>"]

    kb = [
        [InlineKeyboardButton(text="🔥 Top", callback_data=NewsCB(s="top").pack()),
         InlineKeyboardButton(text="📊 Sentiment", callback_data=SentimentCB().pack()),
         InlineKeyboardButton(text="📅 Calendar", callback_data=CalendarCB().pack())],
        [InlineKeyboardButton(text="💹 BTC markets", callback_data=MarketCB(coin="BTC").pack()),
         InlineKeyboardButton(text="⚙️ Settings", callback_data=SettingsCB().pack()),
         InlineKeyboardButton(text="🔄 Refresh", callback_data=HomeCB().pack())],
    ]
    if stories:
        kb.insert(0, [InlineKeyboardButton(text=f"{i}. {badge(n)}", callback_data=ItemCB(id=n.id, s="top", new=1).pack())
                      for i, n in enumerate(stories, 1)])
    return "\n".join(lines), InlineKeyboardMarkup(inline_keyboard=kb)
