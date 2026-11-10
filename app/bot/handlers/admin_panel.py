import asyncio
import logging
import secrets

from aiogram import Bot, F, Router, html
from aiogram.exceptions import TelegramForbiddenError, TelegramRetryAfter
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, InlineKeyboardMarkup, Message
from aiogram.utils.callback_answer import CallbackAnswer
from aiogram.utils.keyboard import InlineKeyboardBuilder
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.bot.callbacks import AdminCb, AdminTicketCb, AdminUserCb, Nav
from app.bot.filters import IsAdmin
from app.bot.keyboards import SUPPORT_TOPICS, btn
from app.bot.states import AdminStates
from app.bot.ui import render
from app.db.models import Promo, SupportTicket, TicketStatus, User
from app.money import fmt, parse_rub
from app.services import shop
from app.services.smm_api import SmmApiError, SmmPanelClient

router = Router(name="admin_panel")
router.message.filter(IsAdmin())
router.callback_query.filter(IsAdmin())
log = logging.getLogger(__name__)

# Keep references so background broadcasts are not garbage-collected.
_background: set[asyncio.Task[None]] = set()


def panel_kb() -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    kb.row(btn("📊 Статистика", AdminCb(action="stats")), btn("🔎 Пользователь", AdminCb(action="find_user")))
    kb.row(btn("🗂 Каталог", AdminCb(action="catalog")), btn("📮 Рассылка", AdminCb(action="broadcast")))
    kb.row(btn("🎁 Промокод", AdminCb(action="promo")), btn("🆘 Обращения", AdminCb(action="tickets")))
    kb.row(btn("🔙 В меню", Nav(to="main")))
    return kb.as_markup()


def back_kb(action: str = "panel") -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[[btn("🔙 Назад", AdminCb(action=action))]])


@router.callback_query(AdminCb.filter(F.action == "panel"))
async def panel(call: CallbackQuery, state: FSMContext) -> None:
    await state.clear()
    await render(call, "<b>👑 Админ-панель</b>", panel_kb())


@router.message(Command("admin"))
async def panel_cmd(message: Message, state: FSMContext) -> None:
    await state.clear()
    await message.answer("<b>👑 Админ-панель</b>", reply_markup=panel_kb())


@router.callback_query(AdminCb.filter(F.action == "stats"))
async def stats(call: CallbackQuery, session: AsyncSession, smm: SmmPanelClient | None) -> None:
    s = await shop.collect_stats(session)
    smm_balance = "не подключена"
    if smm is not None:
        try:
            smm_balance = await smm.balance()
        except SmmApiError:
            smm_balance = "ошибка запроса"
    await render(
        call,
        "<b>📊 Статистика</b>\n\n"
        f"👤 Пользователей: <b>{s.users}</b> (с балансом: {s.users_with_balance})\n"
        f"💳 Пополнено всего: <b>{fmt(s.deposited_total)}</b>\n"
        f"💰 Сумма балансов: <b>{fmt(s.balances_total)}</b>\n\n"
        f"📦 Продаж товаров: <b>{s.product_orders}</b> на {fmt(s.product_revenue)}\n"
        f"📈 Заказов накрутки: <b>{s.smm_orders}</b> на {fmt(s.smm_revenue)}\n\n"
        f"🏦 Баланс SMM-панели: <code>{html.quote(smm_balance)}</code>",
        back_kb(),
    )


# ---------------------------------------------------------------- users


def user_card(user: User, referrals: int) -> tuple[str, InlineKeyboardMarkup]:
    text = (
        f"<b>👤 Пользователь</b> <code>{user.id}</code> @{user.username or '—'}\n\n"
        f"💰 Баланс: <b>{fmt(user.balance)}</b>\n"
        f"💳 Пополнено: {fmt(user.total_deposited)}\n"
        f"🎁 Бонус к пополнению: {user.deposit_bonus_percent}%\n"
        f"👥 Рефералов: {referrals}, заработано {fmt(user.referral_earned)}\n"
        f"🔗 Пригласил: {user.referrer_id or '—'}\n"
        f"🚫 Заблокирован: {'да' if user.is_banned else 'нет'}"
    )
    kb = InlineKeyboardBuilder()
    kb.row(
        btn("➕ Выдать", AdminUserCb(action="add", user_id=user.id)),
        btn("➖ Списать", AdminUserCb(action="sub", user_id=user.id)),
    )
    kb.row(
        btn("🎁 Бонус %", AdminUserCb(action="bonus", user_id=user.id)),
        btn("✅ Разбанить" if user.is_banned else "🚫 Забанить", AdminUserCb(action="ban", user_id=user.id)),
    )
    kb.row(btn("🔙 Назад", AdminCb(action="panel")))
    return text, kb.as_markup()


async def show_user(target: Message | CallbackQuery, session: AsyncSession, user_id: int) -> None:
    user = await session.get(User, user_id, populate_existing=True)
    if user is None:
        await render(target, "❗️ Пользователь не найден.", back_kb())
        return
    text, markup = user_card(user, await shop.count_referrals(session, user.id))
    await render(target, text, markup)


@router.callback_query(AdminCb.filter(F.action == "find_user"))
async def find_user_ask(call: CallbackQuery, state: FSMContext) -> None:
    await state.set_state(AdminStates.find_user)
    await render(call, "🔎 Отправьте ID или @username пользователя:", back_kb())


@router.message(AdminStates.find_user, F.text)
async def find_user(message: Message, state: FSMContext, session: AsyncSession) -> None:
    assert message.text is not None
    query = message.text.strip()
    if query.isdigit():
        user_id: int | None = int(query)
    else:
        user_id = await session.scalar(select(User.id).where(User.username == query.lstrip("@")))
    if user_id is None:
        await message.answer("❗️ Не найден. Попробуйте ещё раз.", reply_markup=back_kb())
        return
    await state.clear()
    await show_user(message, session, user_id)


@router.callback_query(AdminUserCb.filter(F.action.in_({"add", "sub"})))
async def balance_ask(call: CallbackQuery, callback_data: AdminUserCb, state: FSMContext) -> None:
    await state.set_state(AdminStates.balance_change)
    await state.update_data(user_id=callback_data.user_id, sign=1 if callback_data.action == "add" else -1)
    verb = "выдать" if callback_data.action == "add" else "списать"
    await render(call, f"Сколько рублей {verb}?", back_kb())


@router.message(AdminStates.balance_change, F.text)
async def balance_change(message: Message, state: FSMContext, session: AsyncSession, bot: Bot) -> None:
    assert message.text is not None
    amount = parse_rub(message.text)
    if amount is None:
        await message.answer("❗️ Введите сумму числом.", reply_markup=back_kb())
        return
    data = await state.get_data()
    await state.clear()
    if not await shop.change_balance(session, data["user_id"], amount * data["sign"]):
        await message.answer("❗️ Недостаточно средств на балансе пользователя.")
    else:
        await session.commit()
        if data["sign"] > 0:
            try:
                await bot.send_message(data["user_id"], f"💰 Вам начислено {fmt(amount)}.")
            except Exception:  # noqa: BLE001
                log.info("Could not notify user %s", data["user_id"])
    await show_user(message, session, data["user_id"])


@router.callback_query(AdminUserCb.filter(F.action == "bonus"))
async def bonus_ask(call: CallbackQuery, callback_data: AdminUserCb, state: FSMContext) -> None:
    await state.set_state(AdminStates.bonus)
    await state.update_data(user_id=callback_data.user_id)
    await render(call, "🎁 Введите бонус к пополнениям в процентах (0–100):", back_kb())


@router.message(AdminStates.bonus, F.text)
async def bonus_set(message: Message, state: FSMContext, session: AsyncSession) -> None:
    assert message.text is not None
    text = message.text.strip()
    if not text.isdigit() or int(text) > 100:
        await message.answer("❗️ Введите число от 0 до 100.", reply_markup=back_kb())
        return
    user_id = (await state.get_data())["user_id"]
    await state.clear()
    user = await session.get(User, user_id)
    if user is not None:
        user.deposit_bonus_percent = int(text)
        await session.commit()
    await show_user(message, session, user_id)


@router.callback_query(AdminUserCb.filter(F.action == "ban"))
async def toggle_ban(call: CallbackQuery, callback_data: AdminUserCb, session: AsyncSession) -> None:
    user = await session.get(User, callback_data.user_id)
    if user is not None:
        user.is_banned = not user.is_banned
        await session.commit()
    await show_user(call, session, callback_data.user_id)


# ---------------------------------------------------------------- broadcast


@router.callback_query(AdminCb.filter(F.action == "broadcast"))
async def broadcast_ask(call: CallbackQuery, state: FSMContext) -> None:
    await state.set_state(AdminStates.broadcast)
    await render(call, "📮 Отправьте сообщение для рассылки (текст, фото, видео — что угодно):", back_kb())


@router.message(AdminStates.broadcast)
async def broadcast_preview(message: Message, state: FSMContext, session: AsyncSession) -> None:
    await state.update_data(chat_id=message.chat.id, message_id=message.message_id)
    total = len((await session.scalars(select(User.id).where(User.is_banned.is_(False)))).all())
    kb = InlineKeyboardBuilder()
    kb.row(btn(f"✅ Отправить ({total})", AdminCb(action="broadcast_go")), btn("❌ Отмена", AdminCb(action="panel")))
    await message.answer("Отправить это сообщение всем пользователям?", reply_markup=kb.as_markup())


async def run_broadcast(bot: Bot, user_ids: list[int], chat_id: int, message_id: int, admin_id: int) -> None:
    sent = failed = 0
    for user_id in user_ids:
        try:
            await bot.copy_message(user_id, chat_id, message_id)
            sent += 1
        except TelegramRetryAfter as exc:
            await asyncio.sleep(exc.retry_after)
            try:
                await bot.copy_message(user_id, chat_id, message_id)
                sent += 1
            except Exception:  # noqa: BLE001
                failed += 1
        except TelegramForbiddenError:
            failed += 1
        except Exception:  # noqa: BLE001
            log.exception("Broadcast to %s failed", user_id)
            failed += 1
        await asyncio.sleep(0.05)  # stay under Telegram's ~30 msg/s limit
    await bot.send_message(admin_id, f"📮 Рассылка завершена.\n✅ Доставлено: {sent}\n❌ Не доставлено: {failed}")


@router.callback_query(AdminCb.filter(F.action == "broadcast_go"))
async def broadcast_go(
    call: CallbackQuery, state: FSMContext, session: AsyncSession, bot: Bot, callback_answer: CallbackAnswer
) -> None:
    data = await state.get_data()
    await state.clear()
    if "message_id" not in data:
        callback_answer.text = "Сообщение не найдено, начните заново"
        return
    user_ids = list((await session.scalars(select(User.id).where(User.is_banned.is_(False)))).all())
    task = asyncio.create_task(run_broadcast(bot, user_ids, data["chat_id"], data["message_id"], call.from_user.id))
    _background.add(task)
    task.add_done_callback(_background.discard)
    await render(call, f"📮 Рассылка запущена на {len(user_ids)} пользователей.", back_kb())


# ---------------------------------------------------------------- promo


@router.callback_query(AdminCb.filter(F.action == "promo"))
async def promo_ask(call: CallbackQuery, state: FSMContext) -> None:
    await state.set_state(AdminStates.promo)
    await render(
        call,
        "🎁 Отправьте: <code>сумма [активаций] [КОД]</code>\n\n"
        "Примеры:\n<code>100</code> — 100 ₽, 1 активация, случайный код\n"
        "<code>50 20 SAKURA</code> — 50 ₽, 20 активаций, код SAKURA",
        back_kb(),
    )


@router.message(AdminStates.promo, F.text)
async def promo_create(message: Message, state: FSMContext, session: AsyncSession) -> None:
    assert message.text is not None
    parts = message.text.split()
    amount = parse_rub(parts[0]) if parts else None
    uses = parts[1] if len(parts) > 1 else "1"
    code = parts[2] if len(parts) > 2 else secrets.token_urlsafe(9)
    if amount is None or not uses.isdigit() or int(uses) < 1 or len(code) > 64:
        await message.answer("❗️ Неверный формат. Пример: <code>50 20 SAKURA</code>", reply_markup=back_kb())
        return
    if await session.scalar(select(Promo.id).where(Promo.code == code)):
        await message.answer("❗️ Такой код уже существует.", reply_markup=back_kb())
        return
    session.add(Promo(code=code, amount=amount, max_activations=int(uses), activations=0))
    await session.commit()
    await state.clear()
    await message.answer(
        f"✅ Промокод <code>{html.quote(code)}</code> на {fmt(amount)}, активаций: {uses}", reply_markup=back_kb()
    )


# ---------------------------------------------------------------- tickets


@router.callback_query(AdminCb.filter(F.action == "tickets"))
async def tickets(call: CallbackQuery, state: FSMContext, session: AsyncSession) -> None:
    await state.clear()
    items = (
        await session.scalars(
            select(SupportTicket).where(SupportTicket.status == TicketStatus.OPEN).order_by(SupportTicket.id).limit(30)
        )
    ).all()
    kb = InlineKeyboardBuilder()
    for t in items:
        kb.row(
            btn(f"#{t.id} {SUPPORT_TOPICS.get(t.topic, t.topic)} — {t.user_id}", AdminTicketCb(action="view", id=t.id))
        )
    kb.row(btn("🔙 Назад", AdminCb(action="panel")))
    await render(call, f"<b>🆘 Открытые обращения: {len(items)}</b>", kb.as_markup())


@router.callback_query(AdminTicketCb.filter(F.action == "view"))
async def ticket_view(
    call: CallbackQuery, callback_data: AdminTicketCb, session: AsyncSession, callback_answer: CallbackAnswer
) -> None:
    ticket = await session.get(SupportTicket, callback_data.id)
    if ticket is None or ticket.status != TicketStatus.OPEN:
        callback_answer.text = "Обращение уже закрыто"
        return
    kb = InlineKeyboardBuilder()
    kb.row(
        btn("✍️ Ответить", AdminTicketCb(action="answer", id=ticket.id)),
        btn("✅ Закрыть", AdminTicketCb(action="close", id=ticket.id)),
    )
    kb.row(btn("🔙 К обращениям", AdminCb(action="tickets")))
    await render(
        call,
        f"<b>🆘 Обращение #{ticket.id}</b> ({SUPPORT_TOPICS.get(ticket.topic, ticket.topic)})\n"
        f"От: <code>{ticket.user_id}</code>\n\n{html.quote(ticket.text)}",
        kb.as_markup(),
    )


@router.callback_query(AdminTicketCb.filter(F.action == "close"))
async def ticket_close(
    call: CallbackQuery, callback_data: AdminTicketCb, session: AsyncSession, state: FSMContext
) -> None:
    ticket = await session.get(SupportTicket, callback_data.id)
    if ticket is not None:
        ticket.status = TicketStatus.CLOSED
        await session.commit()
    await tickets(call, state, session)


@router.callback_query(AdminTicketCb.filter(F.action == "answer"))
async def ticket_answer_ask(call: CallbackQuery, callback_data: AdminTicketCb, state: FSMContext) -> None:
    await state.set_state(AdminStates.ticket_answer)
    await state.update_data(ticket_id=callback_data.id)
    await render(call, f"✍️ Напишите ответ на обращение #{callback_data.id}:", back_kb("tickets"))


@router.message(AdminStates.ticket_answer, F.text)
async def ticket_answer(message: Message, state: FSMContext, session: AsyncSession, bot: Bot) -> None:
    assert message.text is not None
    ticket = await session.get(SupportTicket, (await state.get_data())["ticket_id"])
    await state.clear()
    if ticket is None or ticket.status != TicketStatus.OPEN:
        await message.answer("❗️ Обращение уже закрыто.", reply_markup=back_kb("tickets"))
        return
    ticket.answer, ticket.status = message.text, TicketStatus.CLOSED
    await session.commit()
    try:
        await bot.send_message(
            ticket.user_id,
            f"📩 <b>Ответ поддержки на обращение #{ticket.id}</b>\n\n"
            f"<i>{html.quote(ticket.text[:500])}</i>\n\n{html.quote(message.text)}",
        )
        result = "✅ Ответ отправлен."
    except Exception:  # noqa: BLE001
        result = "⚠️ Ответ сохранён, но пользователь недоступен (заблокировал бота?)."
    await message.answer(result, reply_markup=back_kb("tickets"))
