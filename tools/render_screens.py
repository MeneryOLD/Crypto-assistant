"""Render the bot's real screens (live data) as Telegram-style PNGs for the README.

    pip install playwright          # uses the Microsoft Edge or Chrome already installed
    python -m tools.render_screens  # reads data/bot.db (a copy), writes docs/screenshots/*.png

Every screenshot is the exact text and buttons the bot would send, drawn in a Telegram-like dark
chat bubble. No real chats are captured, so nothing private ends up in the images.
"""

from __future__ import annotations

import asyncio
import re
import shutil
import tempfile
from html import escape
from pathlib import Path

from aiogram.types import InlineKeyboardMarkup
from playwright.async_api import async_playwright

from analysis.patterns import PatternCache
from analysis.similar import SimilarityEngine
from bot import insights, screens
from bot import settings_ui as ui
from bot.context import App
from collectors.exchanges import ExchangeHub
from collectors.fear_greed import FearGreedClient
from collectors.market_data import MarketDataClient
from collectors.news import NewsCollector
from config import Settings
from storage.db import Database, User

OUT = Path("docs/screenshots")

CSS = """
@import url('https://fonts.googleapis.com/css2?family=Noto+Color+Emoji&display=block');
* { box-sizing: border-box; }
body { margin: 0; background: #0e1621;
       font: 15px/1.38 "Segoe UI", system-ui, -apple-system, "Noto Color Emoji", sans-serif;
       color: #f5f5f5; -webkit-font-smoothing: antialiased; }
.bars { font-family: "Segoe UI Symbol", "DejaVu Sans", sans-serif; letter-spacing: 0; white-space: nowrap; color: #8fc7ff; }
.spark { display: inline-flex; align-items: flex-end; gap: 2px; height: 22px; vertical-align: -4px; }
.spark i { display: block; width: 7px; background: #8fc7ff; border-radius: 1px; }
#shot { width: 470px; padding: 0 0 14px; background: #0e1621; }
.header { display: flex; align-items: center; gap: 10px; padding: 10px 14px; background: #17212b;
          border-bottom: 1px solid #0b1118; }
.avatar { width: 38px; height: 38px; border-radius: 50%; background: linear-gradient(135deg,#5ec2f7,#3a7bd5);
          display: flex; align-items: center; justify-content: center; font-weight: 700; color: #fff; }
.name { font-weight: 600; } .sub { color: #7f91a4; font-size: 13px; }
.chat { padding: 12px 12px 0; }
.user { display: flex; justify-content: flex-end; margin-bottom: 8px; }
.user span { background: #2b5278; padding: 7px 11px; border-radius: 12px 12px 4px 12px; }
.bubble { background: #182533; border-radius: 12px 12px 12px 4px; padding: 9px 12px 10px; word-wrap: break-word; }
.bubble a { color: #6ab3f3; text-decoration: none; }
.bubble pre { background: #0f1923; border-radius: 6px; padding: 6px 8px; margin: 4px 0; white-space: pre;
              font: 12.5px/1.35 Consolas, "Cascadia Mono", monospace; overflow: hidden; }
.bubble code { font-family: Consolas, monospace; background: #0f1923; padding: 0 3px; border-radius: 3px; }
.bubble blockquote { margin: 4px 0; padding: 2px 8px; border-left: 3px solid #6ab3f3; background: #1d2f40;
                     border-radius: 4px; color: #d7e3ee; }
.kb { margin-top: 4px; }
.row { display: flex; gap: 4px; margin-top: 4px; }
.btn { flex: 1; text-align: center; background: rgba(43, 82, 120, 0.75); border-radius: 8px; padding: 8px 4px;
       font-size: 13.5px; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
.menu { margin: 12px 12px 0; padding: 6px; background: #17212b; border-radius: 10px; }
.menu .btn { background: #232e3c; }
"""


SPARK = re.compile("[▁-█]+")  # ▁▂▃▄▅▆▇█ sparkline
GAUGE = re.compile("[▰▱]+")  # ▰▱ gauge


def _spark(m: re.Match[str]) -> str:
    """Draw a sparkline as CSS columns: system fonts render ▁…█ at nearly equal heights."""
    cols = "".join(f'<i style="height:{(ord(c) - 0x2580) * 2.5:.1f}px"></i>' for c in m.group(0))
    return f'<span class="spark">{cols}</span>'


def tg_html_to_web(text: str) -> str:
    """Telegram's HTML subset -> browser HTML (newlines become <br> except inside <pre>)."""
    text = text.replace("<blockquote expandable>", "<blockquote>")
    parts = re.split(r"(<pre>.*?</pre>)", text, flags=re.S)
    html = "".join(
        p if p.startswith("<pre>")
        else GAUGE.sub(r'<span class="bars">\g<0></span>', SPARK.sub(_spark, p.replace("\n", "<br>")))
        for p in parts
    )
    # <pre> and <blockquote> are blocks: the newline right next to them is already a line break
    for block in ("pre", "blockquote"):
        html = html.replace(f"<br><{block}>", f"<{block}>").replace(f"</{block}><br>", f"</{block}>")
    return html


def keyboard(kb: InlineKeyboardMarkup | None, css_class: str = "kb") -> str:
    if kb is None:
        return ""
    rows = "".join(
        '<div class="row">' + "".join(f'<div class="btn">{escape(b.text)}</div>' for b in row) + "</div>"
        for row in kb.inline_keyboard
    )
    return f'<div class="{css_class}">{rows}</div>'


MENU = InlineKeyboardMarkup(
    inline_keyboard=[[{"text": b.text, "callback_data": "x"} for b in row] for row in screens.MAIN_MENU.keyboard]  # type: ignore[misc]
)


def page(text: str, kb: InlineKeyboardMarkup | None, user_said: str | None = None, show_menu: bool = False) -> str:
    said = f'<div class="user"><span>{escape(user_said)}</span></div>' if user_said else ""
    menu = keyboard(MENU, "menu") if show_menu else ""
    return (
        f"<html><head><meta charset='utf-8'><style>{CSS}</style></head><body><div id='shot'>"
        "<div class='header'><div class='avatar'>V</div><div><div class='name'>Vexora</div>"
        "<div class='sub'>bot</div></div></div>"
        f"<div class='chat'>{said}<div class='bubble'>{tg_html_to_web(text)}</div>{keyboard(kb)}</div>{menu}"
        "</div></body></html>"
    )


async def collect(app: App) -> dict[str, str]:
    """name -> HTML page, built from the real screen functions."""
    db = app.db
    demo = User(telegram_id=0, tz_offset=60, alerts_on=True, alert_level=7, alert_categories="", digest_on=True,
                digest_hour=8, calendar_alerts=True, quiet_start=23, quiet_end=7, quiet_critical=True, muted_until=None)
    await app.similar.refresh(force=True)
    top = screens.dedupe_stories(await db.top_news(hours=24, limit=200))
    # pick a story that has its BTC reaction recorded, for the article view
    detail = top[0]
    for n in top:
        if len(await db.marks_for(n.id)) >= 6:
            detail = n
            break

    out: dict[str, str] = {}
    out["01-home"] = page(*await ui.home(app, demo), user_said="🏠 Home", show_menu=True)
    btc = await app.hub.ticker(app.hub.names[0], "BTC")
    alert = top[0]
    out["02-alert"] = page(
        screens.alert_message(alert, btc, await app.patterns.for_item(alert), await app.similar.for_item(alert, limit=30)),
        screens.alert_buttons(alert),
    )
    out["03-top"] = page(*await screens.news(db, s="top"), user_said="🔥 Top")
    out["04-story"] = page(*await screens.article(db, detail.id, s="top", similar=app.similar))
    out["05-sentiment"] = page(*await insights.sentiment(app), user_said="📊 Sentiment")
    out["06-similar"] = page(*await insights.similar_for_text(app, "fed rate hike inflation"), user_said="/similar fed rate hike inflation")
    out["07-markets"] = page(*await screens.market(app.hub, "BTC", app.fg), user_said="btc")
    out["08-calendar"] = page(*await insights.calendar(app, demo, 0, 2), user_said="📅 Calendar")
    out["09-settings"] = page(*ui.settings_screen(demo), user_said="⚙️ Settings")
    out["10-setup"] = page(*ui.setup_step(demo, "tz"), user_said="/start")
    return out


async def render(db_copy: Path) -> None:
    db = Database(str(db_copy))
    await db.init()
    app = App(settings=Settings(), db=db, hub=ExchangeHub(["binance", "bybit", "okx", "kraken"]), collector=NewsCollector(),
              fg=FearGreedClient(), market=MarketDataClient(), similar=SimilarityEngine(db), patterns=PatternCache(db))
    try:
        pages = await collect(app)
    finally:
        await app.hub.close()
        await app.market.close()
        await db.close()

    async with async_playwright() as pw:
        for channel in ("msedge", "chrome", None):
            try:
                browser = await (pw.chromium.launch(channel=channel) if channel else pw.chromium.launch())
                break
            except Exception:
                continue
        else:
            raise SystemExit("No Chromium-based browser found: install Edge/Chrome or run `playwright install chromium`")
        tab = await browser.new_page(device_scale_factor=2, viewport={"width": 470, "height": 900})
        for name, html in pages.items():
            await tab.set_content(html, wait_until="networkidle")
            await tab.evaluate("document.fonts.ready")  # wait for the emoji font (flags)
            await tab.locator("#shot").screenshot(path=str(OUT / f"{name}.png"))
            print("saved", OUT / f"{name}.png")
        await browser.close()


def main() -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    tmp = Path(tempfile.mkdtemp())
    try:
        shutil.copy("data/bot.db", tmp / "bot.db")  # never touch the live database
        asyncio.run(render(tmp / "bot.db"))
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    main()
