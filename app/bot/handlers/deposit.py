import logging

from aiogram import Bot, F, Router
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, LabeledPrice, Message, PreCheckoutQuery
from aiogram.utils.callback_answer import CallbackAnswer
from sqlalchemy.ext.asyncio import AsyncSession

from app.bot import keyboards as kb
from app.bot.callbacks import CheckPaymentCb, Nav, PayMethodCb
from app.bot.states import DepositStates
from app.bot.ui import log_event, render
from app.config import Settings
from app.db.models import Payment, PaymentStatus, User
from app.money import fmt, kopecks_to_stars, parse_rub
from app.services import shop
from app.services.cryptobot import CryptoBotClient, CryptoBotError

router = Router(name="deposit")
log = logging.getLogger(__name__)

STARS_PAYLOAD_PREFIX = "deposit:"


@router.callback_query(Nav.filter(F.to == "deposit"))
async def ask_amount(
    call: CallbackQuery,
    state: FSMContext,
    settings: Settings,
    cryptobot: CryptoBotClient | None,
    callback_answer: CallbackAnswer,
) -> None:
    if cryptobot is None and settings.stars_rub_rate <= 0:
        callback_answer.text, callback_answer.show_alert = "Пополнение временно недоступно", True
        return
    await state.set_state(DepositStates.amount)
    await render(
        call,
        f"💳 Введите сумму пополнения в рублях (минимум {settings.min_deposit_rub} ₽):",
        kb.back("main", "🔙 Отмена"),
    )


@router.message(DepositStates.amount, F.text)
async def choose_method(
    message: Message, state: FSMContext, settings: Settings, cryptobot: CryptoBotClient | None
) -> None:
    assert message.text is not None
    amount = parse_rub(message.text)
    if amount is None or amount < settings.min_deposit_rub * 100:
        await message.answer(
            f"❗️ Введите сумму числом, не меньше {settings.min_deposit_rub} ₽.",
            reply_markup=kb.back("main", "🔙 Отмена"),
        )
        return
    await state.update_data(amount=amount)
    await state.set_state(None)
    await message.answer(
        f"Сумма: <b>{fmt(amount)}</b>\n\nВыберите способ оплаты:",
        reply_markup=kb.pay_methods(cryptobot is not None, settings.stars_rub_rate > 0),
    )


@router.callback_query(PayMethodCb.filter())
async def create_invoice(
    call: CallbackQuery,
    callback_data: PayMethodCb,
    state: FSMContext,
    session: AsyncSession,
    user: User,
    settings: Settings,
    bot: Bot,
    cryptobot: CryptoBotClient | None,
    callback_answer: CallbackAnswer,
) -> None:
    amount: int | None = (await state.get_data()).get("amount")
    if not amount:
        callback_answer.text = "Начните пополнение заново"
        return
    await state.clear()

    if callback_data.method == "cryptobot" and cryptobot is not None:
        payment = await shop.create_payment(session, user.id, "cryptobot", amount)
        try:
            invoice = await cryptobot.create_invoice(
                amount,
                description=f"Пополнение баланса {settings.shop_name}",
                payload=str(payment.id),
                expires_in=settings.invoice_ttl_minutes * 60,
            )
        except CryptoBotError:
            log.exception("CryptoBot invoice creation failed")
            await shop.expire_payment(session, payment.id)
            callback_answer.text, callback_answer.show_alert = "Не удалось создать счёт, попробуйте позже", True
            return
        payment.external_id, payment.pay_url = str(invoice.invoice_id), invoice.url
        await session.commit()
        await render(
            call,
            f"🪙 <b>Счёт #{payment.id} на {fmt(amount)}</b>\n\n"
            "Оплатите счёт в @CryptoBot любой доступной криптовалютой, затем нажмите «Я оплатил».\n"
            f"Счёт действует {settings.invoice_ttl_minutes} мин. Оплата также зачислится автоматически.",
            kb.invoice(invoice.url, payment.id),
        )
        return

    if callback_data.method == "stars" and settings.stars_rub_rate > 0:
        stars = kopecks_to_stars(amount, settings.stars_rub_rate)
        payment = await shop.create_payment(session, user.id, "stars", amount)
        await bot.send_invoice(
            chat_id=user.id,
            title="Пополнение баланса",
            description=f"{fmt(amount)} на баланс {settings.shop_name}",
            payload=f"{STARS_PAYLOAD_PREFIX}{payment.id}",
            currency="XTR",
            prices=[LabeledPrice(label=fmt(amount), amount=stars)],
        )
        return

    callback_answer.text = "Способ оплаты недоступен"


async def notify_credit(bot: Bot, settings: Settings, result: shop.CreditResult, username: str | None) -> None:
    payment = result.payment
    bonus = f" (+{fmt(result.bonus)} бонус)" if result.bonus else ""
    await bot.send_message(
        payment.user_id,
        f"✅ <b>Платёж получен!</b>\nНа баланс зачислено {fmt(result.credited)}{bonus}.",
        reply_markup=kb.back("main", "🏠 В меню"),
    )
    if result.referrer_id and result.referral_reward:
        try:
            await bot.send_message(
                result.referrer_id, f"💸 Ваш реферал пополнил баланс. Вам начислено {fmt(result.referral_reward)}."
            )
        except Exception:  # noqa: BLE001 - referrer may have blocked the bot
            log.info("Could not notify referrer %s", result.referrer_id)
    await log_event(
        bot,
        settings,
        f"🤑 <b>Пополнение #{payment.id}</b> ({payment.provider})\n"
        f"Сумма: {fmt(payment.amount)}{bonus}\nПользователь: <code>{payment.user_id}</code> @{username or '—'}",
    )


@router.callback_query(CheckPaymentCb.filter())
async def check_payment(
    call: CallbackQuery,
    callback_data: CheckPaymentCb,
    session: AsyncSession,
    user: User,
    settings: Settings,
    bot: Bot,
    cryptobot: CryptoBotClient | None,
    callback_answer: CallbackAnswer,
) -> None:
    payment = await session.get(Payment, callback_data.id)
    if payment is None or payment.user_id != user.id:
        callback_answer.text = "Счёт не найден"
        return
    if payment.status == PaymentStatus.PAID:
        callback_answer.text = "✅ Этот счёт уже оплачен и зачислен"
        return
    if payment.status != PaymentStatus.PENDING or cryptobot is None or not payment.external_id:
        callback_answer.text = "Счёт недействителен, создайте новый"
        return
    try:
        invoices = await cryptobot.get_invoices([int(payment.external_id)])
    except CryptoBotError:
        callback_answer.text = "Не удалось проверить оплату, попробуйте позже"
        return
    invoice = invoices.get(int(payment.external_id))
    if invoice is None or invoice.status == "active":
        callback_answer.text, callback_answer.show_alert = "⏳ Оплата пока не поступила", True
        return
    if invoice.status != "paid":
        await shop.expire_payment(session, payment.id)
        callback_answer.text, callback_answer.show_alert = "Счёт истёк, создайте новый", True
        return
    result = await shop.complete_payment(session, payment.id, settings.referral_percent)
    if result:
        await render(call, f"✅ Счёт #{payment.id} оплачен.", kb.back("main", "🏠 В меню"))
        await notify_credit(bot, settings, result, user.username)


# ---------------------------------------------------------------- Telegram Stars


def _payment_id(payload: str) -> int | None:
    if not payload.startswith(STARS_PAYLOAD_PREFIX):
        return None
    tail = payload.removeprefix(STARS_PAYLOAD_PREFIX)
    return int(tail) if tail.isdigit() else None


@router.pre_checkout_query()
async def pre_checkout(query: PreCheckoutQuery, session: AsyncSession, settings: Settings) -> None:
    payment_id = _payment_id(query.invoice_payload)
    payment = await session.get(Payment, payment_id) if payment_id else None
    ok = (
        payment is not None
        and payment.status == PaymentStatus.PENDING
        and payment.user_id == query.from_user.id
        and settings.stars_rub_rate > 0
        and query.total_amount == kopecks_to_stars(payment.amount, settings.stars_rub_rate)
    )
    if ok:
        await query.answer(ok=True)
    else:
        await query.answer(ok=False, error_message="Счёт недействителен, создайте новый.")


@router.message(F.successful_payment)
async def successful_payment(message: Message, session: AsyncSession, user: User, settings: Settings, bot: Bot) -> None:
    sp = message.successful_payment
    assert sp is not None
    payment_id = _payment_id(sp.invoice_payload)
    if payment_id is None:
        return
    result = await shop.complete_payment(
        session, payment_id, settings.referral_percent, external_id=sp.telegram_payment_charge_id
    )
    if result:
        await notify_credit(bot, settings, result, user.username)
