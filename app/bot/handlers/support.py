from aiogram import Bot, F, Router, html
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message
from aiogram.utils.callback_answer import CallbackAnswer
from sqlalchemy.ext.asyncio import AsyncSession

from app.bot import keyboards as kb
from app.bot.callbacks import AdminTicketCb, Nav, SupportTopicCb
from app.bot.states import SupportStates
from app.bot.ui import render
from app.config import Settings
from app.db.models import User
from app.services import shop
from app.services.errors import ShopError

router = Router(name="support")


@router.callback_query(Nav.filter(F.to == "support"))
async def support_menu(call: CallbackQuery, state: FSMContext, settings: Settings) -> None:
    await state.clear()
    await render(call, "<b>🆘 Поддержка</b>\n\nВыберите тему обращения:", kb.support(settings.support_username))


@router.callback_query(SupportTopicCb.filter())
async def ask_question(
    call: CallbackQuery,
    callback_data: SupportTopicCb,
    state: FSMContext,
    session: AsyncSession,
    user: User,
    callback_answer: CallbackAnswer,
) -> None:
    if callback_data.topic not in kb.SUPPORT_TOPICS:
        return
    if await shop.has_open_ticket(session, user.id):
        callback_answer.text = "❗️ У вас уже есть открытое обращение. Дождитесь ответа."
        callback_answer.show_alert = True
        return
    await state.set_state(SupportStates.text)
    await state.update_data(topic=callback_data.topic)
    await render(
        call, "✍️ Опишите проблему одним сообщением (номер заказа, что случилось):", kb.back("main", "🔙 Отмена")
    )


@router.message(SupportStates.text, F.text)
async def receive_question(
    message: Message, state: FSMContext, session: AsyncSession, user: User, settings: Settings, bot: Bot
) -> None:
    assert message.text is not None
    topic = (await state.get_data()).get("topic", "other")
    try:
        ticket = await shop.open_ticket(session, user.id, topic, message.text[:3000])
    except ShopError as exc:
        await state.clear()
        await message.answer(str(exc), reply_markup=kb.back())
        return
    await state.clear()
    await message.answer(
        f"✅ Обращение #{ticket.id} отправлено. Мы ответим здесь, в боте.", reply_markup=kb.back("main", "🏠 В меню")
    )
    markup = kb.InlineKeyboardMarkup(
        inline_keyboard=[[kb.btn("✍️ Ответить", AdminTicketCb(action="view", id=ticket.id))]]
    )
    for admin_id in settings.admin_ids:
        try:
            await bot.send_message(
                admin_id,
                f"🆘 <b>Новое обращение #{ticket.id}</b> ({kb.SUPPORT_TOPICS.get(topic, topic)})\n"
                f"От: <code>{user.id}</code> @{user.username or '—'}\n\n{html.quote(ticket.text)}",
                reply_markup=markup,
            )
        except Exception:  # noqa: BLE001, S112 - admin may not have started the bot
            continue
