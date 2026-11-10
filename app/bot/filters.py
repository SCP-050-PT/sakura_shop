from aiogram.filters import BaseFilter
from aiogram.types import TelegramObject
from aiogram.types import User as TgUser

from app.config import Settings


class IsAdmin(BaseFilter):
    async def __call__(self, event: TelegramObject, settings: Settings, event_from_user: TgUser | None = None) -> bool:
        return event_from_user is not None and settings.is_admin(event_from_user.id)
