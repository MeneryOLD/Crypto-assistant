"""Convenience features: first-run setup, settings, quiet hours, pausing alerts, home dashboard, shortcuts."""

from datetime import datetime, timedelta

import pytest

from bot import settings_ui as ui
from bot.handlers import shortcut
from bot.notifier import wants
from storage.db import Database, NewsItem, User, utcnow
from tests.test_insights import _app


def _buttons(kb):
    return [b for row in kb.inline_keyboard for b in row]


def _check_buttons(kb):
    for b in _buttons(kb):
        assert b.url or len(b.callback_data.encode()) <= 64, b.callback_data


def _user(**kw) -> User:
    defaults = dict(telegram_id=1, tz_offset=0, alerts_on=True, alert_level=7, alert_categories="", digest_on=True,
                    digest_hour=8, calendar_alerts=True, quiet_start=-1, quiet_end=-1, quiet_critical=True, muted_until=None)
    defaults.update(kw)
    return User(**defaults)


def _news(importance: int) -> NewsItem:
    return NewsItem(id=1, title="x", category="economy", importance=importance, source="s", url="u", title_hash="h", event_at=utcnow())


# --- quiet hours & pause --------------------------------------------------------------------


@pytest.mark.parametrize(
    ("start", "end", "hour", "quiet"),
    [(23, 7, 23, True), (23, 7, 3, True), (23, 7, 7, False), (23, 7, 12, False), (0, 8, 0, True), (0, 8, 8, False), (-1, -1, 3, False)],
)
def test_quiet_hours_wrap_midnight(start, end, hour, quiet):
    u = _user(quiet_start=start, quiet_end=end, tz_offset=180)  # UTC+3
    now = datetime(2026, 11, 10, hour, 30) - timedelta(minutes=180)  # local `hour`:30
    assert u.is_quiet(now) is quiet


def test_next_local_hour():
    u = _user(tz_offset=120)
    now = datetime(2026, 11, 10, 20, 0)  # 22:00 local
    assert u.next_local_hour(now, 8) == datetime(2026, 11, 11, 6, 0)  # 08:00 local tomorrow
    assert u.next_local_hour(datetime(2026, 11, 10, 4, 0), 8) == datetime(2026, 11, 10, 6, 0)  # later today


def test_wants_respects_pause_and_quiet_hours():
    now = datetime(2026, 11, 10, 2, 0)  # 02:00 UTC
    night = _user(quiet_start=23, quiet_end=7)
    assert not wants(night, _news(8), now)  # important, but quiet hours
    assert wants(night, _news(9), now)  # critical still comes through
    assert not wants(_user(quiet_start=23, quiet_end=7, quiet_critical=False), _news(10), now)
    paused = _user(muted_until=now + timedelta(hours=1))
    assert not wants(paused, _news(10), now)
    assert wants(_user(muted_until=now - timedelta(minutes=1)), _news(8), now)  # pause expired


def test_pause_until():
    u = _user(tz_offset=0, digest_hour=9)
    assert ui.pause_until(u, 0) is None
    one_hour = ui.pause_until(u, 60)
    assert timedelta(minutes=59) < one_hour - utcnow() <= timedelta(minutes=60)
    morning = ui.pause_until(u, -1)
    assert morning.hour == 9 and morning.minute == 0 and morning > utcnow()


# --- screens ---------------------------------------------------------------------------------


def test_setup_steps():
    u = _user()
    text, kb = ui.setup_step(u, "tz")
    assert "Step 1 of 3" in text and len(_buttons(kb)) == len(ui.TZ_CHOICES) + 1
    assert any("UTC+5:30" in b.text for b in _buttons(kb))
    _check_buttons(kb)
    for step in ("alerts", "digest"):
        text, kb = ui.setup_step(u, step)
        assert "of 3" in text
        _check_buttons(kb)
    done = ui.setup_done(_user(tz_offset=180, digest_hour=7))
    assert "UTC+3" in done and "07:00" in done


def test_settings_and_quiet_screens():
    text, kb = ui.settings_screen(_user(tz_offset=60, quiet_start=23, quiet_end=7))
    assert "UTC+1" in text and "23:00–07:00" in text and "🔕 Pause 1 h" in [b.text for b in _buttons(kb)]
    _check_buttons(kb)
    paused = _user(muted_until=utcnow() + timedelta(hours=2))
    text, kb = ui.settings_screen(paused)
    assert "paused until" in text and "▶️ Resume alerts" in [b.text for b in _buttons(kb)]
    text, kb = ui.quiet_screen(_user(quiet_start=23, quiet_end=7))
    assert "• 23–07 •" in [b.text for b in _buttons(kb)]
    _check_buttons(kb)


@pytest.fixture
async def db():
    d = Database(":memory:")
    await d.init()
    yield d
    await d.close()


async def test_home_renders_without_market_data(db):
    user, _ = await db.upsert_user(1, "a", "Sam")
    text, kb = await ui.home(_app(db), user)
    assert "Vexora" in text and "Sam" in text and "Alerts" in text
    assert "🔄 Refresh" in [b.text for b in _buttons(kb)]
    _check_buttons(kb)


async def test_new_users_start_not_onboarded(db):
    user, created = await db.upsert_user(5, "n", "New")
    assert created and not user.onboarded and user.quiet_start == -1 and user.muted_until is None
    user = await db.update_user(5, onboarded=True, quiet_start=22, quiet_end=7)
    assert user.onboarded and user.is_quiet(datetime(2026, 1, 1, 23, 0))


@pytest.mark.parametrize(
    ("text", "screen"),
    [("settings", "settings"), ("Настройки", "settings"), ("mood", "sentiment"), ("календарь", "calendar"),
     ("top!", "top"), ("help", "help"), ("bitcoin", None), ("fed rate", None)],
)
def test_shortcuts(text, screen):
    assert shortcut(text) == screen
