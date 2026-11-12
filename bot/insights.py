"""Screens for sentiment, the economic calendar, the daily digest, similar past events and admin."""

from __future__ import annotations

from datetime import datetime, timedelta

from aiogram.filters.callback_data import CallbackData
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from analysis.patterns import patterns, topics_of
from analysis.sentiment import gauge, headline_tone, sparkline
from analysis.similar import HORIZONS, MIN_EVENTS, Outlook
from bot.context import App, MarketView, market_view
from bot.formatting import CATEGORY_ICONS, ago, ex_name, fmt_big, fmt_pct, fmt_price, h, updated_line
from bot.screens import BACK_TO_SETTINGS, ItemCB, NewsCB, Screen, _rows, _source_icon, _tab, badge, dedupe_stories
from collectors.calendar import FLAGS, IMPACT_ICON
from collectors.history import plan
from storage.db import CalendarEventRow, User, utcnow

DISCLAIMER = "<i>Signals, not financial advice. Past reactions don't guarantee future ones.</i>"
COIN_ICONS = {"BTC": "₿", "ETH": "Ξ", "SOL": "◎"}
HZ = {60: "1h", 240: "4h", 1440: "24h"}


class SentimentCB(CallbackData, prefix="sn"):
    refresh: int = 0


class CalendarCB(CallbackData, prefix="cl"):
    week: int = 0  # 0 = this week, 1 = next week
    imp: int = 3  # minimum impact shown
    toggle: int = 0  # 1 = toggle reminders


class DigestCB(CallbackData, prefix="dg"):
    action: str = "show"  # show | on | off | hour | tz | send
    value: int = 0


class AdminCB(CallbackData, prefix="ad"):
    action: str = "show"


# --- helpers ---------------------------------------------------------------------


def _pct_short(v: float | None) -> str:
    return "—" if v is None else f"{v:+.1f}%".replace("-", "−")


def _event_line(e: CalendarEventRow, user: User | None, now: datetime) -> str:
    local = user.local(e.starts_at) if user else e.starts_at
    details = " · ".join(x for x in (f"fc {h(e.forecast)}" if e.forecast else "", f"prev {h(e.previous)}" if e.previous else "") if x)
    if e.starts_at <= now:
        when = "✅"
    elif e.starts_at - now < timedelta(hours=3):
        minutes = int((e.starts_at - now).total_seconds() // 60)
        when = f"⏰ in {minutes // 60}h {minutes % 60:02d}m" if minutes >= 60 else f"⏰ in {minutes}m"
    else:
        when = ""
    flag = FLAGS.get(e.country, e.country)
    return f"{local:%H:%M} {IMPACT_ICON.get(e.impact, '')} {flag} <b>{h(e.title)}</b>" + (f" · <i>{details}</i>" if details else "") + (f" {when}" if when else "")


def _mood_lines(view: MarketView, snapshots_mood: list[float]) -> list[str]:
    if view.mood is None:
        return ["🌡 Market mood: <i>not enough data yet</i>"]
    lines = [
        f"🌡 <b>Market mood: {view.mood.score:.0f}/100 · {view.mood.label}</b>",
        gauge(view.mood.score, 20),
    ]
    rows = [(name, f"{value:.0f}") for name, value in view.mood.components.items()]
    width = max(len(n) for n, _ in rows)
    table = "\n".join(f"{n:<{width}}  {v:>3}  {gauge(float(v), 8)}" for n, v in rows)
    lines.append(f"<pre>{h(table)}</pre>")
    if len(snapshots_mood) >= 6:
        lines.append(f"<i>Mood, last {len(snapshots_mood)} h: {sparkline(snapshots_mood[-48:], 0, 100)}</i>")
    return lines


# --- sentiment ---------------------------------------------------------------------


async def sentiment(app: App) -> Screen:
    view = await market_view(app)
    snaps = await app.db.snapshots(days=7)
    lines = ["📊 <b>Market sentiment</b>", ""]
    lines += _mood_lines(view, [s.mood for s in snaps if s.mood is not None])

    fg = view.fear_greed
    if fg:
        parts = [f"yesterday {fg.yesterday}" if fg.yesterday is not None else "",
                 f"week ago {fg.week_ago}" if fg.week_ago is not None else "",
                 f"month ago {fg.month_ago}" if fg.month_ago is not None else "",
                 f"30d avg {fg.avg_30d:.0f}" if fg.avg_30d is not None else ""]
        lines += [
            "",
            f"{fg.emoji} <b>Fear &amp; Greed: {fg.value} · {h(fg.label)}</b>",
            " · ".join(p for p in parts if p),
        ]
        if len(fg.history) >= 30:
            lines.append(f"<i>Last 30 days:</i> {sparkline(list(reversed(fg.history[:30])), 0, 100)}")

    if view.news:
        n = view.news
        lines += ["", f"📰 <b>News tone (24 h): {n.index:.0f}/100</b> · 🟢 {n.bullish} bullish · 🔴 {n.bearish} bearish"]
        cats = [
            f"{CATEGORY_ICONS.get(c, '')} {s.index:.0f}"
            for c, s in sorted(view.news_by_category.items(), key=lambda x: -(x[1].bullish + x[1].bearish))
            if s.bullish + s.bearish >= 3
        ]
        if cats:
            lines.append("By category: " + " · ".join(cats))
        if n.top_bullish:
            lines.append(f"🟢 <a href=\"{h(n.top_bullish.url)}\">{h(n.top_bullish.title[:110])}</a>")
        if n.top_bearish:
            lines.append(f"🔴 <a href=\"{h(n.top_bearish.url)}\">{h(n.top_bearish.title[:110])}</a>")

    lines += ["", "⚖️ <b>Futures positioning (BTC)</b>"]
    if view.funding:
        per = " · ".join(f"{ex_name(e)} {r * 100:+.4f}%".replace("-", "−") for e, r in view.funding.items())
        lines.append(f"Funding (8h): <b>{view.funding_avg_pct:+.4f}%</b> avg ({per})".replace("-", "−"))
    p = view.positioning
    if p:
        if p.long_pct is not None:
            lines.append(f"Binance accounts long: <b>{p.long_pct * 100:.1f}%</b> (L/S {p.long_short_ratio:.2f})"
                         + (f" · top traders L/S {p.top_trader_ratio:.2f}" if p.top_trader_ratio else ""))
        if p.bybit_buy_ratio is not None:
            lines.append(f"Bybit accounts long: <b>{p.bybit_buy_ratio * 100:.1f}%</b>")
        if p.open_interest_btc is not None:
            lines.append(f"Open interest: <b>{p.open_interest_btc / 1000:,.1f}K BTC</b> ({_pct_short(p.oi_change_24h)} 24h)")
        if p.long_pct and p.long_pct > 0.65 and view.funding_avg_pct and view.funding_avg_pct > 0:
            lines.append("<i>Crowded longs + positive funding: a sharp drop can trigger long liquidations.</i>")
        elif p.long_pct and p.long_pct < 0.45:
            lines.append("<i>Shorts dominate: a sharp rise can trigger a short squeeze.</i>")
    if not view.funding and not p:
        lines.append("<i>unavailable right now</i>")

    extra = []
    if view.liquidity:
        lq = view.liquidity
        extra.append(f"💵 <b>Stablecoins</b> ${fmt_big(lq.supply)} ({_pct_short(lq.change_7d)} 7d · {_pct_short(lq.change_30d)} 30d)"
                     " <i>— fresh money ready to buy</i>")
    if view.global_market:
        g = view.global_market
        extra.append(f"🌍 <b>Crypto market</b> ${fmt_big(g.total_mcap)} ({_pct_short(g.mcap_change_24h)} 24h) · "
                     f"BTC dominance {g.btc_dominance:.1f}% · ETH {g.eth_dominance:.1f}%")
    if view.network and view.network.hashrate_ehs:
        nw = view.network
        extra.append(f"⛓ <b>Bitcoin network</b> fees {nw.fastest_fee} sat/vB · hashrate {nw.hashrate_ehs:,.0f} EH/s"
                     + (f" · {nw.tx_24h / 1000:,.0f}K tx/day" if nw.tx_24h else ""))
    if extra:
        lines += ["", *extra]
    lines += ["", updated_line()]

    kb = [
        [
            InlineKeyboardButton(text="🔄 Refresh", callback_data=SentimentCB(refresh=1).pack()),
            InlineKeyboardButton(text="📅 Calendar", callback_data=CalendarCB().pack()),
            InlineKeyboardButton(text="🔥 Top", callback_data=NewsCB(s="top").pack()),
        ]
    ]
    return "\n".join(lines), InlineKeyboardMarkup(inline_keyboard=kb)


# --- calendar --------------------------------------------------------------------------


def _week_bounds(now_local: datetime, week: int) -> tuple[datetime, datetime]:
    monday = (now_local - timedelta(days=now_local.weekday())).replace(hour=0, minute=0, second=0, microsecond=0)
    start = monday + timedelta(weeks=week)
    return start, start + timedelta(days=7)


async def calendar(app: App, user: User, week: int = 0, imp: int = 3) -> Screen:
    now = utcnow()
    start_local, end_local = _week_bounds(user.local(now), week)
    offset = timedelta(minutes=user.tz_offset or 0)
    events = await app.db.calendar_between(start_local - offset, end_local - offset, min_impact=imp)

    title = "this week" if week == 0 else "next week"
    lines = [
        f"📅 <b>Economic calendar · {title}</b>",
        f"<i>Times in {user.tz_label} · {'🔴 high impact only' if imp >= 3 else '🔴 high + 🟠 medium impact'}</i>",
        "",
    ]
    if not events:
        lines.append("No events loaded for this period yet (the schedule for next week is published late in the week).")
    day = None
    for e in events[:60]:
        local = user.local(e.starts_at)
        if local.date() != day:
            day = local.date()
            lines.append(f"\n<b>{local:%a %d %b}</b>" + (" · today" if day == user.local(now).date() else ""))
        lines.append(_event_line(e, user, now))
    if len(events) > 60:
        lines.append(f"<i>…and {len(events) - 60} more</i>")
    lines += [
        "",
        "<i>US CPI, payrolls (NFP), FOMC decisions and PCE usually move BTC the most.</i>",
        f"🔔 Reminders 30 min before 🔴 US releases: <b>{'ON' if user.calendar_alerts else 'OFF'}</b>",
    ]
    kb = [
        [
            InlineKeyboardButton(text=_tab("This week", week == 0), callback_data=CalendarCB(week=0, imp=imp).pack()),
            InlineKeyboardButton(text=_tab("Next week", week == 1), callback_data=CalendarCB(week=1, imp=imp).pack()),
        ],
        [
            InlineKeyboardButton(text=_tab("🔴 High", imp == 3), callback_data=CalendarCB(week=week, imp=3).pack()),
            InlineKeyboardButton(text=_tab("🔴🟠 High + medium", imp == 2), callback_data=CalendarCB(week=week, imp=2).pack()),
        ],
        [
            InlineKeyboardButton(
                text="🔕 Turn reminders off" if user.calendar_alerts else "🔔 Turn reminders on",
                callback_data=CalendarCB(week=week, imp=imp, toggle=1).pack(),
            ),
            InlineKeyboardButton(text="🌍 Time zone", callback_data=DigestCB().pack()),
        ],
    ]
    return "\n".join(lines).replace("\n\n\n", "\n\n"), InlineKeyboardMarkup(inline_keyboard=kb)


def reminder_message(e: CalendarEventRow, user: User) -> str:
    local = user.local(e.starts_at)
    details = " · ".join(x for x in (f"forecast {h(e.forecast)}" if e.forecast else "", f"previous {h(e.previous)}" if e.previous else "") if x)
    return (
        f"⏰ <b>In 30 minutes: {FLAGS.get(e.country, e.country)} {h(e.title)}</b>\n"
        f"{local:%H:%M} ({user.tz_label}) · {IMPACT_ICON.get(e.impact, '')} high impact"
        + (f"\n{details}" if details else "")
        + "\n<i>Big US releases often move BTC within minutes. A reading above forecast for inflation is usually bearish.</i>"
    )


# --- digest -------------------------------------------------------------------------------

HOURS = [6, 7, 8, 9, 10, 12, 18, 20, 22]
TZ_OPTIONS = [-480, -300, -180, 0, 60, 120, 180, 240, 330, 480, 540]  # minutes from UTC


def _tz_name(minutes: int) -> str:
    sign = "+" if minutes >= 0 else "−"
    hours, mins = divmod(abs(minutes), 60)
    return f"UTC{sign}{hours}" + (f":{mins:02d}" if mins else "")


async def digest_settings(app: App, user: User) -> Screen:
    local_now = user.local(utcnow())
    lines = [
        "☀️ <b>Daily digest</b>",
        "",
        f"Status: <b>{'ON ✅' if user.digest_on else 'OFF 🔕'}</b>",
        f"Time: <b>{user.digest_hour:02d}:00</b> in <b>{user.tz_label}</b> (your time now: {local_now:%H:%M})",
        "",
        "The morning brief includes:",
        "• market mood 0–100 and what drives it",
        "• BTC, ETH, SOL with 24 h and 7 d change",
        "• Fear &amp; Greed, futures positioning, stablecoin liquidity",
        "• today's economic releases in your time zone",
        "• the 10 most important stories of the last 24 h with their tone",
        "• what history says about today's main topics",
        "",
        "<i>Pick the hour and your time zone below. The same time zone is used in the calendar.</i>",
    ]
    toggle = (
        InlineKeyboardButton(text="🔕 Turn off", callback_data=DigestCB(action="off").pack())
        if user.digest_on
        else InlineKeyboardButton(text="🔔 Turn on", callback_data=DigestCB(action="on").pack())
    )
    hours = [
        InlineKeyboardButton(text=_tab(f"{hr:02d}:00", hr == user.digest_hour), callback_data=DigestCB(action="hour", value=hr).pack())
        for hr in HOURS
    ]
    zones = [
        InlineKeyboardButton(text=_tab(_tz_name(m), m == user.tz_offset), callback_data=DigestCB(action="tz", value=m).pack())
        for m in TZ_OPTIONS
    ]
    kb = [[toggle, InlineKeyboardButton(text="📨 Send it now", callback_data=DigestCB(action="send").pack())]]
    kb += [*_rows(hours, 5), *_rows(zones, 4), [BACK_TO_SETTINGS]]
    return "\n".join(lines), InlineKeyboardMarkup(inline_keyboard=kb)


async def digest_messages(app: App, user: User) -> list[tuple[str, InlineKeyboardMarkup | None]]:
    """The morning brief: (1) market overview + today's calendar, (2) top stories + what history says."""
    now = utcnow()
    local = user.local(now)
    view = await market_view(app)
    snaps = await app.db.snapshots(days=2)
    day_ago = [s for s in snaps if s.taken_at <= now - timedelta(hours=23)]

    greeting = "Good morning" if 4 <= local.hour < 12 else ("Good afternoon" if local.hour < 18 else "Good evening")
    lines = [f"☀️ <b>{greeting}{', ' + h(user.first_name) if user.first_name else ''}!</b> Your Vexora brief · {local:%a %d %b}", ""]
    if view.mood:
        prev = f" (yesterday {day_ago[-1].mood:.0f})" if day_ago and day_ago[-1].mood is not None else ""
        lines.append(f"🌡 Market mood: <b>{view.mood.score:.0f}/100 · {view.mood.label}</b>{prev}")
        lines.append(gauge(view.mood.score, 20))
    for coin, t in view.tickers.items():
        week = view.change_7d.get(coin)
        lines.append(f"{COIN_ICONS.get(coin, '')} {coin} <b>${fmt_price(t.last)}</b> {fmt_pct(t.change_pct)}" + (f" · 7d {_pct_short(week)}" if week is not None else ""))
    if view.global_market:
        g = view.global_market
        lines.append(f"🌍 Total ${fmt_big(g.total_mcap)} ({_pct_short(g.mcap_change_24h)}) · BTC dominance {g.btc_dominance:.1f}%")
    if view.fear_greed:
        fg = view.fear_greed
        lines.append(f"{fg.emoji} Fear &amp; Greed <b>{fg.value}</b> {h(fg.label)}" + (f" (yesterday {fg.yesterday} · week ago {fg.week_ago})" if fg.week_ago is not None else ""))
    pos = []
    if view.funding_avg_pct is not None:
        pos.append(f"funding {view.funding_avg_pct:+.4f}%".replace("-", "−"))
    if view.positioning and view.positioning.long_pct is not None:
        pos.append(f"{view.positioning.long_pct * 100:.0f}% of Binance accounts long")
    if view.positioning and view.positioning.oi_change_24h is not None:
        pos.append(f"open interest {_pct_short(view.positioning.oi_change_24h)}")
    if pos:
        lines.append("⚖️ " + " · ".join(pos))
    if view.liquidity:
        lines.append(f"💵 Stablecoins ${fmt_big(view.liquidity.supply)} ({_pct_short(view.liquidity.change_7d)} 7d)")
    if view.news:
        lines.append(f"📰 News tone 24 h: <b>{view.news.index:.0f}/100</b> (🟢 {view.news.bullish} · 🔴 {view.news.bearish})")
    from bot.forecast_ui import forecast_line  # local: forecast_ui builds on this module

    if app.forecast and (fline := forecast_line(app.forecast.latest)):
        lines.append(fline)

    day_start_local = local.replace(hour=0, minute=0, second=0, microsecond=0)
    offset = timedelta(minutes=user.tz_offset or 0)
    events = await app.db.calendar_between(day_start_local - offset, day_start_local + timedelta(days=1) - offset, min_impact=2)
    lines += ["", f"📅 <b>Today's releases</b> ({user.tz_label})"]
    if events:
        lines += [_event_line(e, user, now) for e in events[:10]]
    else:
        lines.append("<i>No high- or medium-impact releases today.</i>")
    first = "\n".join(lines)

    top = dedupe_stories(await app.db.top_news(hours=24, limit=200))[:10]
    lines = ["🔥 <b>Top 10 stories · last 24 h</b>", ""]
    for i, n in enumerate(top, 1):
        tone = n.sentiment if n.sentiment is not None else headline_tone(n.title)
        tone_icon = "🟢" if tone > 0 else ("🔴" if tone < 0 else "⚪")
        lines.append(
            f"<b>{i}.</b> {badge(n)} {tone_icon} <a href=\"{h(n.url)}\">{h(n.title)}</a>\n"
            f"<i>{_source_icon(n.source)}{h(n.source)} · {ago(n.event_at)}"
            + (f" · {h(n.importance_reasons.split(' · ')[0])}" if n.importance_reasons else "")
            + "</i>"
        )
    if not top:
        lines.append("<i>No scored news yet.</i>")

    # What history says about today's main topics
    today_topics: dict[str, int] = {}
    for n in top:
        for t in topics_of(n):
            today_topics[t] = today_topics.get(t, 0) + 1
    if today_topics:
        _, groups = patterns(await app.db.all_reactions(days=120), by="topic")
        by_name = {g.name: g for g in groups}
        hist = []
        for t, _ in sorted(today_topics.items(), key=lambda x: -x[1]):
            g = by_name.get(t)
            if g and 240 in g.avg and g.events >= MIN_EVENTS:
                hist.append(
                    f"• {h(t)} ({g.events} past events): BTC {fmt_pct(g.avg[240], dot=False)} avg in 4h, "
                    f"up {g.up_share.get(240, 0) * 100:.0f}% of the time"
                )
            if len(hist) >= 4:
                break
        if hist:
            lines += ["", "🧠 <b>What history says about today's topics</b>", *hist]
    lines += ["", DISCLAIMER]
    kb = InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="📊 Sentiment", callback_data=SentimentCB().pack()),
                InlineKeyboardButton(text="📅 Calendar", callback_data=CalendarCB().pack()),
                InlineKeyboardButton(text="🔥 Top", callback_data=NewsCB(s="top").pack()),
            ],
            [InlineKeyboardButton(text="⚙️ Digest settings", callback_data=DigestCB().pack())],
        ]
    )
    return [(first, None), ("\n".join(lines), kb)]


# --- similar past events -------------------------------------------------------------------------


def _outlook_table(o: Outlook) -> str:
    rows = [("", "median", "average", "up")]
    for hz in HORIZONS:
        if s := o.stats.get(hz):
            rows.append((HZ[hz], _pct_short(s.median), _pct_short(s.mean), f"{s.up_share * 100:.0f}%"))
    if b := o.baseline.get(240):
        rows.append(("any 4h", _pct_short(b.median), _pct_short(b.mean), f"{b.up_share * 100:.0f}%"))
    widths = [max(len(r[i]) for r in rows) for i in range(4)]
    text = "\n".join(f"{r[0]:<{widths[0]}}  {r[1]:>{widths[1]}}  {r[2]:>{widths[2]}}  {r[3]:>{widths[3]}}" for r in rows)
    return f"<pre>{h(text)}</pre>"


def outlook_text(title: str, o: Outlook | None, index_size: int) -> str:
    lines = ["🧠 <b>What happened after similar news</b>", f"<i>“{h(title[:160])}”</i>", ""]
    if o is None or 240 not in o.stats or o.stats[240].n < MIN_EVENTS:
        found = o.events if o else 0
        lines += [
            f"Only {found} similar past event{'s' if found != 1 else ''} found — not enough for a signal.",
            f"<i>The memory holds {index_size:,} past events with known BTC reactions and grows every hour.</i>",
        ]
        return "\n".join(lines)
    s4 = o.stats[240]
    lines.append(f"Signal: <b>{o.signal}</b> · {o.confidence} confidence")
    lines.append(_outlook_table(o))
    lines.append(f"Range after 4h: {_pct_short(s4.worst)} … {_pct_short(s4.best)}")
    if (vol := o.volatility_ratio) is not None:
        if vol >= 1.3:
            lines.append(f"⚡ Moves after such news were <b>{vol:.1f}×</b> bigger than after average news.")
        elif vol <= 0.8:
            lines.append(f"😴 Moves after such news were smaller than usual ({vol:.1f}×).")
    lines.append(f"<i>Based on {o.events} similar events from {index_size:,} in memory (last 120 days, one per 30 min).</i>")
    lines += ["", "<b>Most similar past events</b>"]
    for ex in o.examples[:8]:
        r = ex.reaction
        c4, c24 = r.changes.get(240), r.changes.get(1440)
        lines.append(
            f"• {r.item.event_at:%d %b} · 4h {fmt_pct(c4)} · 24h {fmt_pct(c24)}\n"
            f"  <a href=\"{h(r.item.url)}\">{h(r.item.title[:100])}</a> <i>({h(r.item.source)})</i>"
        )
    lines += ["", DISCLAIMER]
    return "\n".join(lines)


async def similar_for_item(app: App, news_id: int) -> Screen:
    item = await app.db.get_news(news_id)
    back = [InlineKeyboardButton(text="◀️ Back to story", callback_data=ItemCB(id=news_id, s="top").pack())]
    if item is None:
        return "This story is no longer stored.", InlineKeyboardMarkup(inline_keyboard=[back])
    outlook = await app.similar.for_item(item)
    kb = [[*back, InlineKeyboardButton(text="🔥 Top", callback_data=NewsCB(s="top").pack())]]
    return outlook_text(item.title, outlook, app.similar.size), InlineKeyboardMarkup(inline_keyboard=kb)


async def similar_for_text(app: App, text: str) -> Screen:
    outlook = await app.similar.for_text(text)
    return outlook_text(text, outlook, app.similar.size), None


# --- admin -----------------------------------------------------------------------------------


async def history_progress(app: App) -> tuple[int, int]:
    keys = await app.db.kv_keys("history:")
    full = plan(utcnow())
    return sum(1 for k, *_ in full if k in keys), len(full)


async def admin(app: App) -> Screen:
    st = await app.db.stats()
    done, total = await history_progress(app)
    lines = [
        "🛠 <b>Admin</b>",
        "",
        f"👥 Users: <b>{st['users']:.0f}</b> · active 7d {st['users_active_7d']:.0f} · alerts on {st['users_alerts']:.0f} · digest on {st['users_digest']:.0f}",
        f"📰 News stored: <b>{st['news_total']:,.0f}</b> (archive {st['news_history']:,.0f}) · new 24h {st['news_24h']:,.0f} · unscored {st['news_unscored']:,.0f}",
        f"📈 Price points: {st['price_marks']:,.0f} · 🧠 events in similarity index: {app.similar.size:,}",
        f"📅 Calendar events: {st['calendar_events']:.0f} · 🌡 mood snapshots: {st['snapshots']:.0f}",
        f"🔔 Sent 24h: {st['alerts_24h']:.0f} alerts · {st['reminders_24h']:.0f} reminders",
        f"🗄 Database: {st.get('db_mb', 0)} MB",
        f"📚 History import: {done}/{total} archive requests" + (" ✅" if done >= total else " (running in background)"),
    ]
    if app.scheduler:
        lines += ["", "<b>Background jobs</b>"]
        for j in app.scheduler.jobs.values():
            icon = "✅" if j.healthy else "❌"
            lines.append(f"{icon} <code>{h(j.name)}</code> · every {int(j.every)}s · ok {j.ago()} · runs {j.runs} · fails {j.failures}")
            if not j.healthy and j.last_error:
                lines.append(f"   <i>{h(j.last_error[:120])}</i>")
    failing = [(name, s) for name, s in app.collector.status.items() if s.ok is False] if app.collector else []
    if failing:
        lines += ["", f"<b>Sources failing ({len(failing)})</b>"]
        lines += [f"❌ {h(n)}: {h(s.error)}" for n, s in failing[:12]]
    lines.append(updated_line())
    kb = [[InlineKeyboardButton(text="🔄 Refresh", callback_data=AdminCB().pack())]]
    return "\n".join(lines), InlineKeyboardMarkup(inline_keyboard=kb)


def import_progress_line(done: int, total: int) -> str:
    return f"📚 History archive: {done}/{total} imported" + (" ✅" if done >= total else " (in progress)")
