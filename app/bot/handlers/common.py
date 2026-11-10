from aiogram import Bot, F, Router, html
from aiogram.filters import Command, CommandObject, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message
from sqlalchemy.ext.asyncio import AsyncSession

from app.bot import keyboards as kb
from app.bot.callbacks import Nav
from app.bot.ui import log_event, render, send_main_screen
from app.config import Settings
from app.db.models import User
from app.money import fmt
from app.services import shop

router = Router(name="common")


def main_text(user: User) -> str:
    return f"<b>💳 Ваш баланс:</b> <code>{fmt(user.balance)}</code>"


@router.message(CommandStart(), F.chat.type == "private")
async def cmd_start(
    message: Message,
    command: CommandObject,
    state: FSMContext,
    session: AsyncSession,
    user: User,
    user_created: bool,
    settings: Settings,
    bot: Bot,
) -> None:
    await state.clear()
    if user_created:
        tg = message.from_user
        assert tg is not None
        referrer_id = int(command.args) if command.args and command.args.isdigit() else None
        if referrer_id and await shop.set_referrer(session, user, referrer_id):
            await session.commit()
            await bot.send_message(referrer_id, f"🎉 У вас новый реферал: {html.quote(tg.full_name)}")
        await log_event(
            bot,
            settings,
            f"🔔 <b>Новый пользователь</b>\n{html.quote(tg.full_name)} (@{tg.username or '—'}) <code>{tg.id}</code>",
        )
    await message.answer(f"🌸 Добро пожаловать в <b>{html.quote(settings.shop_name)}</b>!", reply_markup=kb.reply_menu)
    await send_main_screen(bot, message.chat.id, settings, main_text(user), kb.main_menu(settings.is_admin(user.id)))


@router.message(F.text == kb.MENU_BUTTON_TEXT)
@router.message(Command("menu"))
async def menu_message(message: Message, state: FSMContext, user: User, settings: Settings, bot: Bot) -> None:
    await state.clear()
    await send_main_screen(bot, message.chat.id, settings, main_text(user), kb.main_menu(settings.is_admin(user.id)))


@router.callback_query(Nav.filter(F.to == "main"))
async def menu_callback(
    call: CallbackQuery, state: FSMContext, session: AsyncSession, user: User, settings: Settings
) -> None:
    await state.clear()
    user = await shop.refresh_user(session, user.id)
    await render(call, main_text(user), kb.main_menu(settings.is_admin(user.id)))


@router.message(Command("id"))
async def cmd_id(message: Message) -> None:
    await message.answer(f"ID чата: <code>{message.chat.id}</code>")


@router.callback_query(Nav.filter(F.to == "info"))
async def info(call: CallbackQuery, settings: Settings) -> None:
    await render(
        call,
        f"<b>🌸 {html.quote(settings.shop_name)}</b>\n\n"
        "⭐️ Цифровые товары с моментальной выдачей\n"
        "⭐️ Автоматическая накрутка для соцсетей\n"
        "⭐️ Замена товара в случае невалида\n"
        "⭐️ Поддержка прямо в боте",
        kb.back(),
    )


@router.callback_query(Nav.filter(F.to == "referral"))
async def referral(call: CallbackQuery, session: AsyncSession, user: User, settings: Settings, bot: Bot) -> None:
    me = await bot.me()
    user = await shop.refresh_user(session, user.id)
    count = await shop.count_referrals(session, user.id)
    await render(
        call,
        "<b>🤝 Партнёрская программа</b>\n\n"
        f"🔗 Ваша ссылка:\n<code>https://t.me/{me.username}?start={user.id}</code>\n\n"
        f"👥 Рефералов: <b>{count}</b>\n"
        f"🌀 Процент: <b>{settings.referral_percent}%</b>\n"
        f"💸 Заработано: <b>{fmt(user.referral_earned)}</b>\n\n"
        f"Вы получаете {settings.referral_percent}% от каждого пополнения приглашённых.",
        kb.back(),
    )
