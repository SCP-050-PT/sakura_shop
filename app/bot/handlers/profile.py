from aiogram import Bot, F, Router, html
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message
from aiogram.utils.callback_answer import CallbackAnswer
from sqlalchemy.ext.asyncio import AsyncSession

from app.bot import keyboards as kb
from app.bot.callbacks import Nav, OrderCb
from app.bot.handlers.delivery import deliver_order
from app.bot.states import PromoStates
from app.bot.ui import log_event, render
from app.config import Settings
from app.db.models import CategoryKind, Order, User
from app.money import fmt
from app.services import shop
from app.services.errors import ShopError
from app.services.smm_api import SmmApiError, SmmPanelClient

router = Router(name="profile")

STATUS_TITLES = {
    "pending": "⏳ Оформляется",
    "processing": "⏳ В работе",
    "completed": "✅ Выполнен",
    "partial": "🟡 Выполнен частично",
    "failed": "❌ Отменён",
}


@router.callback_query(Nav.filter(F.to == "profile"))
async def profile(call: CallbackQuery, state: FSMContext, session: AsyncSession, user: User) -> None:
    await state.clear()
    user = await shop.refresh_user(session, user.id)
    bonus = f"\n🎁 Бонус к пополнению: <b>{user.deposit_bonus_percent}%</b>" if user.deposit_bonus_percent else ""
    await render(
        call,
        "<b>👤 Профиль</b>\n\n"
        f"🆔 ID: <code>{user.id}</code>\n"
        f"💰 Баланс: <b>{fmt(user.balance)}</b>\n"
        f"💳 Всего пополнено: <b>{fmt(user.total_deposited)}</b>{bonus}",
        kb.profile(),
    )


@router.callback_query(Nav.filter(F.to == "orders"))
async def orders(call: CallbackQuery, session: AsyncSession, user: User, callback_answer: CallbackAnswer) -> None:
    items = await shop.user_orders(session, user.id)
    if not items:
        callback_answer.text = "У вас пока нет заказов"
        return
    await render(call, "<b>🧾 Последние заказы</b>", kb.orders(items))


def order_text(order: Order) -> str:
    lines = [
        f"<b>🧾 Заказ #{order.id}</b>",
        f"📦 {html.quote(order.title)} × {order.quantity}",
        f"💳 Сумма: {fmt(order.amount)}",
        f"📅 {order.created_at:%d.%m.%Y %H:%M}",
        f"Статус: {STATUS_TITLES.get(order.status, order.status)}",
    ]
    if order.kind == CategoryKind.SMM:
        lines.insert(2, f"🔗 {html.quote(order.link or '')}")
        if order.panel_status:
            lines.append(f"Статус в сервисе: <code>{html.quote(order.panel_status)}</code>")
    if order.refunded:
        lines.append(f"↩️ Возвращено: {fmt(order.refunded)}")
    return "\n".join(lines)


@router.callback_query(OrderCb.filter())
async def order_view(
    call: CallbackQuery,
    callback_data: OrderCb,
    session: AsyncSession,
    user: User,
    bot: Bot,
    smm: SmmPanelClient | None,
    callback_answer: CallbackAnswer,
) -> None:
    order = await session.get(Order, callback_data.id)
    if order is None or order.user_id != user.id:
        callback_answer.text = "Заказ не найден"
        return
    if callback_data.action == "items":
        await deliver_order(bot, user.id, order)
        return
    if callback_data.action == "refresh":
        if smm is None:
            callback_answer.text = "Сервис накрутки недоступен"
            return
        try:
            order = await shop.sync_smm_order(session, smm, order)
        except SmmApiError:
            callback_answer.text = "Не удалось получить статус, попробуйте позже"
            return
        callback_answer.text = "Статус обновлён"
    await render(call, order_text(order), kb.order(order))


@router.callback_query(Nav.filter(F.to == "promo"))
async def promo_ask(call: CallbackQuery, state: FSMContext) -> None:
    await state.set_state(PromoStates.code)
    await render(call, "🎁 Введите промокод:", kb.back("profile", "🔙 Отмена"))


@router.message(PromoStates.code, F.text)
async def promo_activate(
    message: Message, state: FSMContext, session: AsyncSession, user: User, settings: Settings, bot: Bot
) -> None:
    assert message.text is not None
    try:
        amount = await shop.activate_promo(session, user.id, message.text)
    except ShopError as exc:
        await message.answer(str(exc), reply_markup=kb.back("profile", "🔙 Отмена"))
        return
    await state.clear()
    await message.answer(
        f"🎁 Промокод активирован! На баланс начислено <b>{fmt(amount)}</b>.", reply_markup=kb.back("profile")
    )
    await log_event(
        bot,
        settings,
        f"🎁 <b>Промокод</b> <code>{html.quote(message.text.strip())}</code> на {fmt(amount)}\n"
        f"Пользователь: <code>{user.id}</code> @{user.username or '—'}",
    )
