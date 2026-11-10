"""Opens a DB session per update and loads/saves the Telegram user."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any

from aiogram import BaseMiddleware
from aiogram.types import TelegramObject
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app import services


class DbUserMiddleware(BaseMiddleware):
    def __init__(self, session_factory: async_sessionmaker[AsyncSession]):
        self._session_factory = session_factory

    async def __call__(
        self,
        handler: Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: dict[str, Any],
    ) -> Any:
        async with self._session_factory() as session:
            data["session"] = session
            tg_user = data.get("event_from_user")
            if tg_user is not None:
                data["user"] = await services.upsert_user(
                    session,
                    telegram_id=tg_user.id,
                    # Alerts go to the user's private chat (its id equals the user id),
                    # even if they used a command inside a group.
                    chat_id=tg_user.id,
                    username=tg_user.username,
                    first_name=tg_user.first_name,
                    last_name=tg_user.last_name,
                )
            return await handler(event, data)
