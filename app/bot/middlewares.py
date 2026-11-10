import time
from collections.abc import Awaitable, Callable
from typing import Any

from aiogram import BaseMiddleware
from aiogram.types import CallbackQuery, Message, TelegramObject
from aiogram.types import User as TgUser
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.config import Settings
from app.services.shop import get_or_create_user

Handler = Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]]


class DbSessionMiddleware(BaseMiddleware):
    """Opens a session per update and registers the sender as a shop user."""

    def __init__(self, sessionmaker: async_sessionmaker[AsyncSession]) -> None:
        self.sessionmaker = sessionmaker

    async def __call__(self, handler: Handler, event: TelegramObject, data: dict[str, Any]) -> Any:
        async with self.sessionmaker() as session:
            data["session"] = session
            tg_user: TgUser | None = data.get("event_from_user")
            if tg_user is not None and not tg_user.is_bot:
                user, created = await get_or_create_user(session, tg_user.id, tg_user.username)
                await session.commit()
                settings: Settings = data["settings"]
                if user.is_banned and not settings.is_admin(user.id):
                    return None
                data["user"] = user
                data["user_created"] = created
            return await handler(event, data)


class ThrottlingMiddleware(BaseMiddleware):
    def __init__(self, rate: float = 0.5) -> None:
        self.rate = rate
        self._last: dict[int, float] = {}

    async def __call__(self, handler: Handler, event: TelegramObject, data: dict[str, Any]) -> Any:
        if isinstance(event, Message | CallbackQuery) and event.from_user:
            settings: Settings | None = data.get("settings")
            if settings and settings.is_admin(event.from_user.id):
                return await handler(event, data)
            if isinstance(event, Message) and event.successful_payment:
                return await handler(event, data)
            now = time.monotonic()
            last = self._last.get(event.from_user.id, 0.0)
            self._last[event.from_user.id] = now
            if now - last < self.rate:
                if isinstance(event, CallbackQuery):
                    await event.answer("⏳ Не так быстро")
                return None
            if len(self._last) > 10_000:
                cutoff = now - 60
                self._last = {k: v for k, v in self._last.items() if v > cutoff}
        return await handler(event, data)
