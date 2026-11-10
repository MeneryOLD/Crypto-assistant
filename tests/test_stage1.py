"""Core: storage + formatting helpers."""

from bot.formatting import fmt_big, fmt_pct, fmt_price
from storage.db import Database


def test_formatting():
    assert fmt_price(73418.4) == "73,418.40"
    assert fmt_price(2.5) == "2.5"
    assert fmt_price(0.00001234) == "0.00001234"
    assert fmt_big(2.47e9) == "2.5B"
    assert fmt_pct(-1.234) == "🔴 −1.23%"
    assert fmt_pct(-1.234, dot=False) == "−1.23%"
    assert fmt_pct(None) == "—"


async def test_upsert_user():
    db = Database(":memory:")
    await db.init()
    _, created = await db.upsert_user(42, "alice", "Alice")
    user, created_again = await db.upsert_user(42, "alice2", "Alice")
    assert created and not created_again
    assert user.username == "alice2"
    assert await db.count_users() == 1
    await db.close()
