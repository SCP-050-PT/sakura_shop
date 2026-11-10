from contextlib import suppress
from pathlib import Path

from aiogram import Bot, html
from aiogram.exceptions import TelegramBadRequest
from aiogram.types import CallbackQuery, FSInputFile, InlineKeyboardMarkup, Message

from app.config import Settings
from app.db.models import Order
from app.money import fmt

CAPTION_LIMIT = 1024


async def render(target: Message | CallbackQuery, text: str, markup: InlineKeyboardMarkup | None = None) -> None:
    """Show a screen: edit the callback's message in place, or send a new message."""
    if isinstance(target, Message):
        await target.answer(text, reply_markup=markup)
        return

    message = target.message
    if isinstance(message, Message):
        try:
            if message.photo and len(text) <= CAPTION_LIMIT:
                await message.edit_caption(caption=text, reply_markup=markup)
                return
            if message.text is not None:
                await message.edit_text(text, reply_markup=markup)
                return
        except TelegramBadRequest as exc:
            if "message is not modified" in str(exc):
                return
        # Long text over a photo or an uneditable message: replace it.
        with suppress(TelegramBadRequest):
            await message.delete()
    assert target.bot is not None
    await target.bot.send_message(target.from_user.id, text, reply_markup=markup)


async def send_main_screen(bot: Bot, chat_id: int, settings: Settings, text: str, markup: InlineKeyboardMarkup) -> None:
    photo: Path = settings.main_menu_photo
    if photo.is_file():
        await bot.send_photo(chat_id, FSInputFile(photo), caption=text, reply_markup=markup)
    else:
        await bot.send_message(chat_id, text, reply_markup=markup)


async def log_event(bot: Bot, settings: Settings, text: str) -> None:
    if settings.log_chat_id is None:
        return
    with suppress(Exception):
        await bot.send_message(settings.log_chat_id, text)


async def alert_admins(bot: Bot, settings: Settings, text: str) -> None:
    """For events that need an admin's action: DM every admin and mirror to the log chat."""
    for admin_id in settings.admin_ids:
        with suppress(Exception):
            await bot.send_message(admin_id, text)
    await log_event(bot, settings, text)


def review_alert_text(order: Order, reason: str) -> str:
    return (
        f"⚠️ <b>Заказ #{order.id} требует проверки</b>: {reason}.\n"
        f"{html.quote(order.title)} × {order.quantity} = {fmt(order.amount)}\n"
        f"🔗 {html.quote(order.link or '—')}\nПользователь: <code>{order.user_id}</code>\n\n"
        "Найдите заказ в SMM-панели по ссылке и времени, затем:\n"
        f"• нашли — <code>/order_attach {order.id} ID_в_панели</code>\n"
        f"• не нашли — <code>/order_refund {order.id}</code>"
    )
