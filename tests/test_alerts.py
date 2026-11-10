"""Importance scoring, notifications and alert settings (no network)."""

from datetime import timedelta

import pytest
from sqlalchemy import text

from analysis.importance import level, score
from bot import screens
from bot.notifier import Notifier, score_items, wants
from collectors.news import Article
from storage.db import Database, NewsItem, User, utcnow


def item(title: str, source: str = "CoinDesk", category: str = "crypto", id: int = 1, summary: str | None = None) -> NewsItem:
    return NewsItem(id=id, title=title, source=source, category=category, url=f"u{id}", title_hash=str(id),
                    summary=summary, event_at=utcnow(), backfill=False)


# --- scoring ---------------------------------------------------------------


@pytest.mark.parametrize(
    ("title", "source", "category", "low", "high"),
    [
        ("FED CUTS RATES BY 25 BPS, SIGNALS MORE EASING", "Walter Bloomberg", "economy", 8, 10),
        ("SEC approves spot Solana ETF", "SEC", "regulation", 9, 10),
        ("Exchange halts withdrawals after $1.2 billion hack", "The Block", "crypto", 9, 10),
        ("JUST IN: Russia launches missile strike on Kyiv", "Watcher Guru", "politics", 5, 8),
        ("US CPI rises 0.4% in October, above forecast", "FinancialJuice", "economy", 6, 9),
        ("Bitcoin price prediction: could BTC reach $200K?", "CryptoNews", "crypto", 1, 4),
        ("Manchester United sign new striker", "BBC World", "politics", 1, 3),
        ("Opinion: why the dollar matters", "The Economist", "economy", 1, 3),
    ],
)
def test_score_ranges(title, source, category, low, high):
    s = score(item(title, source, category))
    assert low <= s.value <= high, (s.value, s.reasons)


def test_confirmation_by_other_outlets_raises_score():
    main = item("Israel and Hamas agree Gaza ceasefire deal", "BBC World", "politics", 1)
    others = [
        item("Gaza ceasefire deal agreed between Israel and Hamas", src, "politics", i)
        for i, src in enumerate(["Reuters", "Al Jazeera", "CNN World", "Sky News World"], 2)
    ]
    alone, confirmed = score(main), score(main, others)
    assert confirmed.value >= alone.value + 2
    assert "5 outlets" in confirmed.reasons


def test_levels():
    assert level(10) == ("🔴", "Critical")
    assert level(7) == ("🟠", "Important")
    assert level(5) == ("🟡", "Notable")
    assert level(2) == ("⚪", "Minor")


def test_wants_respects_level_and_categories():
    u = User(telegram_id=1, alert_level=7, alert_categories="")
    hot = item("x", category="economy")
    hot.importance = 8
    assert wants(u, hot)
    u.alert_categories = "crypto"
    assert not wants(u, hot)
    hot.importance = 6
    u.alert_categories = ""
    assert not wants(u, hot)


def test_toggle_category():
    assert screens.toggle_category(set(), "crypto") == {"politics", "elections", "economy", "regulation"}
    assert screens.toggle_category({"crypto"}, "crypto") == set()  # nothing left -> back to all
    assert screens.toggle_category({"crypto"}, "economy") == {"crypto", "economy"}


# --- database + notifier -----------------------------------------------------


@pytest.fixture
async def db():
    d = Database(":memory:")
    await d.init()
    yield d
    await d.close()


class FakeBot:
    def __init__(self):
        self.sent: list[tuple[int, str]] = []

    async def send_message(self, chat_id, text, reply_markup=None):
        self.sent.append((chat_id, text))


class FakeHub:
    names = ["binance"]

    async def ticker(self, name, coin):
        raise RuntimeError("offline")


class NoPatterns:
    async def for_item(self, item):
        return None


async def add(db: Database, title: str, url: str, source: str = "Walter Bloomberg", category: str = "economy", minutes_ago: int = 0):
    [n] = await db.add_news([Article(source, category, title, url, None, utcnow() - timedelta(minutes=minutes_ago))])
    return n


async def test_notifier_sends_once_per_story_and_respects_settings(db):
    await db.upsert_user(1, "a", "A")  # defaults: alerts on, level 7
    await db.upsert_user(2, "b", "B")
    await db.update_user(2, alert_level=9)
    await db.upsert_user(3, "c", "C")
    await db.update_user(3, alerts_on=False)

    first = await add(db, "FED CUTS RATES BY 50 BPS IN EMERGENCY MOVE", "u1")
    dup = await add(db, "Fed slashes rates by 50 bps in emergency move, Powell says", "u2", source="CNBC Economy")
    minor = await add(db, "Opinion: what the weather means for markets", "u3")
    await score_items(db, [first, dup, minor])
    assert first.importance >= 9 and minor.importance < 5

    bot = FakeBot()
    notifier = Notifier(bot, db, FakeHub(), NoPatterns())  # type: ignore[arg-type]
    assert await notifier.notify([first, dup, minor]) == 2  # users 1 and 2 get the story once; user 3 is off
    assert sorted(c for c, _ in bot.sent) == [1, 2]
    assert "FED CUTS RATES" in bot.sent[0][1] and "CRITICAL" in bot.sent[0][1]
    assert await notifier.notify([dup]) == 0  # same story already sent


async def test_notifier_skips_old_and_backfill(db):
    await db.upsert_user(1, "a", "A")
    old = await add(db, "FED CUTS RATES BY 50 BPS IN EMERGENCY MOVE", "u1", minutes_ago=300)
    [back] = await db.add_news([Article("SEC", "regulation", "SEC approves spot ETF", "u2", None, utcnow())], backfill=True)
    await score_items(db, [old, back])
    bot = FakeBot()
    assert await Notifier(bot, db, FakeHub(), NoPatterns()).notify([old, back]) == 0  # type: ignore[arg-type]


async def test_top_news_and_settings_screen(db):
    user, _ = await db.upsert_user(1, "a", "A")
    a = await add(db, "Exchange halts withdrawals after $2 billion hack", "u1", "The Block", "crypto")
    b = await add(db, "Bitcoin weekly recap", "u2", "CryptoNews", "crypto")
    await score_items(db, [a, b])
    text_top, kb = await screens.news(db, s="top")
    assert text_top.index("halts withdrawals") < text_top.index("weekly recap")
    assert "• 🔥 Most important •" in [btn.text for row in kb.inline_keyboard for btn in row]

    text_settings, kb = await screens.alerts_settings(db, user)
    assert "Status: <b>ON</b>" in text_settings and "🟠 Important +" in text_settings
    for row in kb.inline_keyboard:
        for btn in row:
            assert btn.url or len(btn.callback_data.encode()) <= 64


async def test_old_database_gets_new_columns(tmp_path):
    path = str(tmp_path / "old.db")
    old = Database(path)
    async with old.engine.begin() as conn:  # the stage-1 schema, before alerts existed
        await conn.execute(text("CREATE TABLE users (id INTEGER PRIMARY KEY, telegram_id BIGINT UNIQUE, username VARCHAR(64), first_name VARCHAR(128), created_at DATETIME, last_seen_at DATETIME)"))
        await conn.execute(text("INSERT INTO users (telegram_id, username) VALUES (7, 'old')"))
    await old.close()

    db = Database(path)
    await db.init()
    user = await db.get_user(7)
    assert user and user.alerts_on and user.alert_level == 7 and user.categories == set()
    await db.close()


async def test_expired_button_press_is_ignored():
    from aiogram.exceptions import TelegramBadRequest
    from aiogram.methods import AnswerCallbackQuery

    from bot.handlers import _ack

    class OldQuery:
        async def answer(self, text=None, show_alert=False):
            raise TelegramBadRequest(AnswerCallbackQuery(callback_query_id="1"), "Bad Request: query is too old and response timeout expired or query ID is invalid")

    await _ack(OldQuery())  # type: ignore[arg-type]  # must not raise
