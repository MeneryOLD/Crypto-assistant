"""🔮 Forecast screen: BTC / ETH / SOL 4 h & 24 h outlook, price ranges, market context, tested reliability, live record."""

from __future__ import annotations

from aiogram.filters.callback_data import CallbackData
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from analysis.forecast import COINS, STEP, Driver, Forecast, Outlook
from analysis.technicals import technicals
from bot.context import App
from bot.formatting import ago, fmt_price, h
from bot.insights import SentimentCB
from bot.screens import NewsCB, Screen


class ForecastCB(CallbackData, prefix="fc"):
    refresh: int = 0
    coin: str = "BTC"


def _driver_text(d: Driver) -> str:
    v = d.value
    if d.name.startswith(("ret_", "dist_")):
        value = f"{v * 100:+.1f}%".replace("-", "−")
    elif d.name == "bb_pos":
        value = f"{v:+.2f}".replace("-", "−")
    elif d.name == "vol_24h":
        value = f"{v * 100:.2f}% per hour"
    elif d.name == "drawdown":
        value = f"{abs(v) * 100:.1f}% below" if v < -0.001 else "at the high"
    elif d.name == "fg":
        value = f"{v:.0f}"
    elif d.name == "fg_change":
        value = f"{v:+.0f} points".replace("-", "−")
    elif d.name == "funding":
        value = f"{v:+.4f}%".replace("-", "−")
    elif d.name == "news_tone":
        value = "bullish" if v > 0.15 else ("bearish" if v < -0.15 else "mixed")
    else:
        value = f"{v:.1f}"
    arrow = "↑" if d.push > 0 else "↓"
    return f"{arrow} {h(d.label)}: {value}"


def _pct(p: float) -> str:
    return f"{p * 100:.0f}%"


def outlook_block(o: Outlook, coin: str = "BTC") -> list[str]:
    name = "Next 4 hours" if o.horizon == 4 else "Next 24 hours"
    lean = "up" if o.prob_up >= 0.5 else "down"
    lines = [f"<b>{name}: {o.verdict}</b>"]
    if o.model.reliable:
        lines.append(f"Chance {h(coin)} is higher: <b>{_pct(o.prob_up)}</b>")
    else:
        lines.append(f"<i>The model leans {lean} ({_pct(max(o.prob_up, 1 - o.prob_up))}), but hasn't proven it beats a coin flip</i>")
    mid = (o.low * o.high) ** 0.5
    width = (o.high / mid - 1) * 100
    lines.append(f"📏 Likely range: <b>${fmt_price(o.low)} – ${fmt_price(o.high)}</b> (±{width:.1f}%) <i>2 in 3 chance</i>")
    if (r := o.range) is not None:
        lines.append(f"Almost surely: ${fmt_price(r.low95)} – ${fmt_price(r.high95)} <i>(19 in 20)</i> · "
                     f"chance of a move over ±{r.big_move_pct:g}%: <b>{_pct(r.big_move_prob)}</b>")
    top = [d for d in o.drivers if abs(d.push) >= 0.05][:3]
    if top:
        lines.append("Main drivers: " + " · ".join(_driver_text(d) for d in top))
    return lines


def forecast_line(fc: Forecast | None) -> str | None:
    """One line for Home and the digest."""
    if fc is None or not fc.outlooks:
        return None
    parts = []
    for hz, o in sorted(fc.outlooks.items()):
        icon = "⚪" if not o.model.reliable else ("🟢" if o.prob_up >= 0.55 else ("🔴" if o.prob_up <= 0.45 else "⚪"))
        parts.append(f"{hz}h {icon} {_pct(o.prob_up)} up")
    edge = any(o.model.reliable for o in fc.outlooks.values())
    return "🔮 Forecast: " + " · ".join(parts) + ("" if edge else " <i>(no proven edge)</i>")


def _keyboard(coin: str) -> InlineKeyboardMarkup:
    tabs = [InlineKeyboardButton(text=f"• {c} •" if c == coin else c, callback_data=ForecastCB(coin=c).pack()) for c in COINS]
    return InlineKeyboardMarkup(
        inline_keyboard=[
            tabs,
            [
                InlineKeyboardButton(text="🔄 Refresh", callback_data=ForecastCB(refresh=1, coin=coin).pack()),
                InlineKeyboardButton(text="📐 Technicals", callback_data=TechCB(coin=coin).pack()),
            ],
            [
                InlineKeyboardButton(text="📊 Sentiment", callback_data=SentimentCB().pack()),
                InlineKeyboardButton(text="🔥 Top news", callback_data=NewsCB(s="top").pack()),
            ],
        ]
    )


async def forecast_screen(app: App, coin: str = "BTC") -> Screen:
    coin = coin.upper() if coin.upper() in COINS else "BTC"
    engine = app.forecast
    fc = engine.get(coin) if engine else None
    if engine and fc is None:
        fc = await engine.run(coin=coin)
    kb = _keyboard(coin)
    if fc is None or not fc.outlooks:
        return f"🔮 The {h(coin)} forecast is being prepared (it learns from 2 years of prices). Try again in a minute.", kb

    lines = [f"🔮 <b>{h(coin)} forecast</b> · <i>updated {ago(fc.made_at)}</i>", f"Price: <b>${fmt_price(fc.price)}</b>", ""]
    for hz in sorted(fc.outlooks):
        lines += [*outlook_block(fc.outlooks[hz], coin), ""]

    lines.append("<b>How reliable is it?</b>")
    for hz, o in sorted(fc.outlooks.items()):
        m = o.model
        lines.append(
            f"Direction {hz}h: right <b>{_pct(m.test_acc)}</b> on the last {m.test_n * STEP // 24} days it never saw "
            f"(always guessing the common direction: {_pct(m.base_acc)}; luck alone: ±{m.margin * 100:.0f}%)"
        )
    for hz, o in sorted(fc.outlooks.items()):
        if (r := o.range) is not None:
            rm = r.model
            lines.append(f"Range {hz}h: the 2-in-3 range held the price <b>{_pct(rm.held68)}</b> of the time on unseen data, "
                         f"the 19-in-20 range {_pct(rm.held95)}")
    for hz in sorted(fc.outlooks):
        scored, correct, clear, clear_ok = await app.db.forecast_record(hz, coin=coin)
        if scored:
            extra = f" · clear calls {clear_ok}/{clear}" if clear else ""
            lines.append(f"Live record {hz}h: {correct}/{scored} correct ({_pct(correct / scored)}){extra}")
    if (first := await app.db.first_forecast_at(coin)) is not None:
        lines.append(f"<i>Live record since {first:%d %b}, scored automatically when each forecast's time is up.</i>")

    if fc.context:
        bull = sum(1 for s in fc.context if s.vote > 0)
        bear = sum(1 for s in fc.context if s.vote < 0)
        lines += ["", f"🌍 <b>Market context</b> · 🟢 {bull} · 🔴 {bear}"]
        group = None
        for s in fc.context:
            if s.group != group:
                group = s.group
                lines.append(f"<u>{h(group)}</u>")
            lines.append(f"{VOTE_ICON[s.vote]} {h(s.name)}: <b>{h(s.value)}</b> — {h(s.meaning)}")
        lines.append("<i>Usual readings, shown for context. Tested on 2 years of data, none of them predicted the next "
                     "4–24 h direction better than chance, so the model doesn't bet on them.</i>")

    if (t := fc.tone) and t.any_up is not None and t.bearish_up is not None:
        lines += [
            "",
            "📰 <b>Does news tone predict the price?</b>",
            f"After bearish headlines BTC was higher 4h later <b>{_pct(t.bearish_up)}</b> of the time (n={t.bearish_n}); "
            + (f"after bullish ones {_pct(t.bullish_up)} (n={t.bullish_n}); " if t.bullish_up is not None else "")
            + f"after any news {_pct(t.any_up)}.",
            "<i>Red headlines are often followed by rises, so tone alone isn't used as a signal.</i>",
        ]
    lines += ["", "<i>Probabilities from past data, not certainties and not financial advice.</i>"]
    text = "\n".join(lines)
    while len(text) > 4000 and len(lines) > 10:  # Telegram's message limit: drop from the end of the context block
        lines.pop(-3)
        text = "\n".join(lines)
    return text, kb


# --- technical analysis screen ---------------------------------------------------------------


class TechCB(CallbackData, prefix="ta"):
    coin: str = "BTC"


TA_COINS = ("BTC", "ETH", "SOL", "BNB", "XRP", "DOGE")
VOTE_ICON = {1: "🟢", -1: "🔴", 0: "⚪"}


async def technicals_screen(app: App, coin: str = "BTC") -> Screen:
    coin = coin.upper()
    tabs = [
        InlineKeyboardButton(text=f"• {c} •" if c == coin else c, callback_data=TechCB(coin=c).pack())
        for c in TA_COINS
    ]
    nav = [
        InlineKeyboardButton(text="🔮 Forecast", callback_data=ForecastCB(coin=coin if coin in COINS else "BTC").pack()),
        InlineKeyboardButton(text="🔄 Refresh", callback_data=TechCB(coin=coin).pack()),
    ]
    kb = InlineKeyboardMarkup(inline_keyboard=[tabs[:3], tabs[3:], nav])
    try:
        t = await technicals(app.hub, coin)
    except Exception:
        return f"⚠️ Couldn't load candles for <b>{h(coin)}</b> right now. Try again in a minute.", kb

    bull, bear, neutral = t.score
    lines = [
        f"📐 <b>{h(coin)} technical analysis</b> · ${fmt_price(t.price)}",
        f"Overall: <b>{t.verdict}</b> — 🟢 {bull} bullish · 🔴 {bear} bearish · ⚪ {neutral} neutral",
    ]
    group = None
    for s in t.signals:
        if s.group != group:
            group = s.group
            lines += ["", f"<b>{h(group)}</b>"]
        lines.append(f"{VOTE_ICON[s.vote]} {h(s.name)}: <b>{h(s.value)}</b> — {h(s.meaning)}")
    lines += [
        "",
        "<i>Indicators describe where the market stands now. Whether they actually predicted BTC's next move is "
        "measured in 🔮 Forecast, which uses them as inputs.</i>",
    ]
    return "\n".join(lines), kb
