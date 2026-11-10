from collections.abc import AsyncGenerator, AsyncIterator
from datetime import datetime
from decimal import Decimal
from itertools import count
from typing import Any, Union, get_args, get_origin

import pytest
from aiogram import Bot, Dispatcher
from aiogram.client.session.base import BaseSession
from aiogram.methods import TelegramMethod
from aiogram.types import CallbackQuery, Chat, Message, PreCheckoutQuery, SuccessfulPayment, Update, User
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.__main__ import build_dispatcher
from app.bot.handlers import admin_catalog, admin_panel, catalog, common, deposit, inline, profile, support
from app.config import Settings
from app.db import create_engine, create_sessionmaker, init_db

ADMIN_ID = 1000
BOT_USER = User(id=42, is_bot=True, first_name="Sakura", username="sakura_test_bot")


class MockSession(BaseSession):
    """Records Bot API calls and returns minimal plausible results."""

    def __init__(self) -> None:
        super().__init__()
        self.requests: list[TelegramMethod[Any]] = []
        self._ids = count(500)

    async def make_request(self, bot: Bot, method: TelegramMethod[Any], timeout: int | None = None) -> Any:
        self.requests.append(method)
        returning = method.__returning__
        options = get_args(returning) if get_origin(returning) is Union else (returning,)
        if Message in options:
            chat_id = getattr(method, "chat_id", None)
            return Message(
                message_id=next(self._ids),
                date=datetime.now(),
                chat=Chat(id=chat_id if isinstance(chat_id, int) else 1, type="private"),
                from_user=BOT_USER,
                text=getattr(method, "text", None) or "x",
            )
        if User in options:
            return BOT_USER
        if bool in options:
            return True
        return None

    async def stream_content(self, *args: Any, **kwargs: Any) -> AsyncGenerator[bytes, None]:
        yield b""

    async def close(self) -> None:
        pass

    def sent(self, method_name: str, chat_id: int | None = None) -> list[Any]:
        return [
            r
            for r in self.requests
            if type(r).__name__ == method_name and (chat_id is None or getattr(r, "chat_id", None) == chat_id)
        ]

    def texts(self, chat_id: int | None = None) -> str:
        parts = []
        for r in self.requests:
            if chat_id is not None and getattr(r, "chat_id", None) not in (chat_id, None):
                continue
            parts.append(str(getattr(r, "text", None) or getattr(r, "caption", None) or ""))
        return "\n".join(parts)


@pytest.fixture
def settings(tmp_path: Any) -> Settings:
    return Settings(
        _env_file=None,
        bot_token="42:TEST",
        admin_ids=[ADMIN_ID],
        database_url=f"sqlite+aiosqlite:///{tmp_path / 'test.db'}",
        main_menu_photo=tmp_path / "missing.jpg",
        stars_rub_rate=Decimal("1.5"),
    )


@pytest.fixture
async def sessionmaker(settings: Settings) -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    engine = create_engine(settings.database_url)
    await init_db(engine)
    yield create_sessionmaker(engine)
    await engine.dispose()


class Harness:
    def __init__(self, dp: Dispatcher, bot: Bot, session: MockSession) -> None:
        self.dp, self.bot, self.api = dp, bot, session
        self._update_ids = count(1)
        self._msg_ids = count(1)

    @staticmethod
    def tg_user(user_id: int) -> User:
        return User(id=user_id, is_bot=False, first_name=f"User{user_id}", username=f"user{user_id}")

    async def feed(self, **kwargs: Any) -> None:
        await self.dp.feed_update(self.bot, Update(update_id=next(self._update_ids), **kwargs))

    async def message(self, user_id: int, text: str | None = None, **extra: Any) -> None:
        msg = Message(
            message_id=next(self._msg_ids),
            date=datetime.now(),
            chat=Chat(id=user_id, type="private"),
            from_user=self.tg_user(user_id),
            text=text,
            **extra,
        )
        await self.feed(message=msg)

    async def callback(self, user_id: int, data: Any) -> None:
        packed = data.pack() if hasattr(data, "pack") else data
        msg = Message(
            message_id=next(self._msg_ids),
            date=datetime.now(),
            chat=Chat(id=user_id, type="private"),
            from_user=BOT_USER,
            text="screen",
        )
        cq = CallbackQuery(
            id=str(next(self._update_ids)),
            from_user=self.tg_user(user_id),
            chat_instance="ci",
            data=packed,
            message=msg,
        )
        await self.feed(callback_query=cq)

    async def pre_checkout(self, user_id: int, payload: str, total: int) -> None:
        q = PreCheckoutQuery(
            id="pcq", from_user=self.tg_user(user_id), currency="XTR", total_amount=total, invoice_payload=payload
        )
        await self.feed(pre_checkout_query=q)

    async def paid(self, user_id: int, payload: str, total: int, charge_id: str = "charge-1") -> None:
        sp = SuccessfulPayment(
            currency="XTR",
            total_amount=total,
            invoice_payload=payload,
            telegram_payment_charge_id=charge_id,
            provider_payment_charge_id="",
        )
        await self.message(user_id, successful_payment=sp)


@pytest.fixture
def make_harness(settings: Settings, sessionmaker: async_sessionmaker[AsyncSession]):  # type: ignore[no-untyped-def]
    def factory(smm: Any = None, cryptobot: Any = None) -> Harness:
        # Module-level routers can belong to one parent only; each test builds a fresh Dispatcher.
        for module in (admin_catalog, admin_panel, catalog, common, deposit, inline, profile, support):
            module.router._parent_router = None  # noqa: SLF001
        session = MockSession()
        bot = Bot("42:TEST", session=session)
        dp = build_dispatcher(settings, sessionmaker, smm=smm, cryptobot=cryptobot)
        # Throttling would drop rapid scripted updates.
        for observer in (dp.message, dp.callback_query):
            observer.outer_middleware._middlewares.clear()  # noqa: SLF001
        return Harness(dp, bot, session)

    return factory
