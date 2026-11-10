import asyncio
import contextlib
import logging
import sys

from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.exceptions import TelegramUnauthorizedError
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import BotCommand
from aiogram.utils.callback_answer import CallbackAnswerMiddleware
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.background import run_background
from app.bot.handlers import setup_routers
from app.bot.middlewares import DbSessionMiddleware, ThrottlingMiddleware
from app.config import Settings
from app.db import create_engine, create_sessionmaker, init_db
from app.services.cryptobot import CryptoBotClient
from app.services.smm_api import SmmPanelClient


def build_dispatcher(
    settings: Settings,
    sessionmaker: async_sessionmaker[AsyncSession],
    smm: SmmPanelClient | None = None,
    cryptobot: CryptoBotClient | None = None,
) -> Dispatcher:
    dp = Dispatcher(storage=MemoryStorage(), settings=settings, smm=smm, cryptobot=cryptobot)
    dp.update.outer_middleware(DbSessionMiddleware(sessionmaker))
    throttling = ThrottlingMiddleware()
    dp.message.outer_middleware(throttling)
    dp.callback_query.outer_middleware(throttling)
    dp.callback_query.middleware(CallbackAnswerMiddleware())
    dp.include_router(setup_routers())
    return dp


async def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        stream=sys.stdout,
    )
    settings = Settings()  # type: ignore[call-arg]
    engine = create_engine(settings.database_url)
    await init_db(engine)
    sessionmaker = create_sessionmaker(engine)

    smm = (
        SmmPanelClient(settings.smm_api_url, settings.smm_api_key.get_secret_value()) if settings.smm_api_key else None
    )
    cryptobot = (
        CryptoBotClient(settings.cryptobot_token.get_secret_value(), testnet=settings.cryptobot_testnet)
        if settings.cryptobot_token
        else None
    )
    bot = Bot(
        settings.bot_token.get_secret_value(),
        default=DefaultBotProperties(parse_mode=ParseMode.HTML, link_preview_is_disabled=True),
    )
    dp = build_dispatcher(settings, sessionmaker, smm, cryptobot)
    background: asyncio.Task[None] | None = None
    try:
        try:
            me = await bot.get_me()
        except TelegramUnauthorizedError:
            logging.error("BOT_TOKEN is invalid: check the token from @BotFather in .env")
            return
        await bot.set_my_commands(
            [
                BotCommand(command="start", description="Главное меню"),
                BotCommand(command="id", description="Узнать ID чата"),
            ]
        )
        background = asyncio.create_task(run_background(sessionmaker, bot, settings, cryptobot, smm))
        logging.info(
            "Bot @%s started. SMM: %s, CryptoBot: %s, Stars: %s",
            me.username,
            bool(smm),
            bool(cryptobot),
            settings.stars_rub_rate > 0,
        )
        await dp.start_polling(bot, allowed_updates=dp.resolve_used_update_types())
    finally:
        if background is not None:
            background.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await background
        for client in (smm, cryptobot):
            if client is not None:
                await client.close()
        await bot.session.close()
        await engine.dispose()


def run() -> None:
    with contextlib.suppress(KeyboardInterrupt):
        asyncio.run(main())


if __name__ == "__main__":
    run()
