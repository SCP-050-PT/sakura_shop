from aiogram import Bot, F, Router, html
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message
from aiogram.utils.callback_answer import CallbackAnswer
from sqlalchemy.ext.asyncio import AsyncSession

from app.bot import keyboards as kb
from app.bot.callbacks import BuyProductCb, BuyServiceCb, CatalogCb, CategoryCb, ServiceCb, SubcategoryCb
from app.bot.handlers.delivery import deliver_order
from app.bot.states import ProductStates, SmmStates
from app.bot.ui import alert_admins, log_event, render, review_alert_text
from app.config import Settings
from app.db.models import Category, CategoryKind, Order, SmmService, Subcategory, User
from app.money import fmt, smm_cost
from app.services import shop
from app.services.errors import OrderNeedsReview, ShopError
from app.services.smm_api import SmmPanelClient

router = Router(name="catalog")

KIND_TITLES = {CategoryKind.PRODUCT: "🛍 Товары", CategoryKind.SMM: "📈 Накрутка"}


def product_text(sub: Subcategory, in_stock: int) -> str:
    return (
        f"<b>📦 {html.quote(sub.name)}</b>\n"
        f"{sub.description}\n\n"
        f"💳 Цена: <b>{fmt(sub.price)}</b> за 1 шт.\n"
        f"♻️ В наличии: <b>{in_stock}</b> шт."
    )


@router.callback_query(CatalogCb.filter())
async def show_catalog(
    call: CallbackQuery,
    callback_data: CatalogCb,
    state: FSMContext,
    session: AsyncSession,
    callback_answer: CallbackAnswer,
) -> None:
    await state.clear()
    kind = CategoryKind(callback_data.kind)
    cats = await shop.list_categories(session, kind)
    if not cats:
        callback_answer.text = "Раздел пока пуст"
        return
    await render(call, f"<b>{KIND_TITLES[kind]}</b>\n\nВыберите категорию:", kb.categories(cats))


@router.callback_query(CategoryCb.filter())
async def show_category(
    call: CallbackQuery,
    callback_data: CategoryCb,
    state: FSMContext,
    session: AsyncSession,
    callback_answer: CallbackAnswer,
) -> None:
    await state.clear()
    cat = await session.get(Category, callback_data.id)
    if cat is None:
        callback_answer.text = "Категория удалена"
        return
    subs = await shop.list_subcategories(session, cat.id)
    await render(call, f"<b>{html.quote(cat.name)}</b>\n\nВыберите подкатегорию:", kb.subcategories(subs, cat.kind))


@router.callback_query(SubcategoryCb.filter())
async def show_subcategory(
    call: CallbackQuery,
    callback_data: SubcategoryCb,
    state: FSMContext,
    session: AsyncSession,
    callback_answer: CallbackAnswer,
) -> None:
    await state.clear()
    sub = await session.get(Subcategory, callback_data.id)
    if sub is None:
        callback_answer.text = "Подкатегория удалена"
        return
    cat = await session.get(Category, sub.category_id)
    assert cat is not None
    if cat.kind == CategoryKind.SMM:
        services = await shop.list_services(session, sub.id)
        description = f"{sub.description}\n\n" if sub.description else ""
        await render(
            call, f"<b>{html.quote(sub.name)}</b>\n\n{description}Выберите услугу:", kb.services(services, sub)
        )
        return
    count = await shop.stock_count(session, sub.id)
    await render(call, product_text(sub, count), kb.product(sub, count))


# ---------------------------------------------------------------- products


@router.callback_query(BuyProductCb.filter())
async def ask_product_quantity(
    call: CallbackQuery,
    callback_data: BuyProductCb,
    state: FSMContext,
    session: AsyncSession,
    user: User,
    callback_answer: CallbackAnswer,
) -> None:
    sub = await session.get(Subcategory, callback_data.sub_id)
    count = await shop.stock_count(session, callback_data.sub_id)
    if sub is None or count == 0:
        callback_answer.text, callback_answer.show_alert = "❗️ Товар закончился", True
        return
    user = await shop.refresh_user(session, user.id)
    affordable = min(count, user.balance // sub.price) if sub.price else count
    if affordable < 1:
        callback_answer.text, callback_answer.show_alert = "❗️ Недостаточно средств. Пополните баланс.", True
        return
    await state.set_state(ProductStates.quantity)
    await state.update_data(sub_id=sub.id)
    await render(
        call,
        f"<b>{html.quote(sub.name)}</b>\n\nМожно купить до <b>{affordable}</b> шт.\n👉 Введите количество:",
        kb.back("main", "🔙 Отмена"),
    )


@router.message(ProductStates.quantity, F.text)
async def buy_product(
    message: Message, state: FSMContext, session: AsyncSession, user: User, settings: Settings, bot: Bot
) -> None:
    assert message.text is not None
    if not message.text.strip().isdigit():
        await message.answer("❗️ Введите число.", reply_markup=kb.back("main", "🔙 Отмена"))
        return
    sub_id = (await state.get_data())["sub_id"]
    try:
        order = await shop.buy_product(session, user.id, sub_id, int(message.text))
    except ShopError as exc:
        await message.answer(str(exc), reply_markup=kb.back("main", "🔙 Отмена"))
        return
    await state.clear()
    await message.answer(
        f"🎉 <b>Покупка #{order.id} оформлена!</b>\nСписано: {fmt(order.amount)}",
        reply_markup=kb.back("main", "🏠 В меню"),
    )
    await deliver_order(bot, user.id, order)
    await log_event(
        bot,
        settings,
        f"📦 <b>Покупка #{order.id}</b>\n{html.quote(order.title)} × {order.quantity} = {fmt(order.amount)}\n"
        f"Пользователь: <code>{user.id}</code> @{user.username or '—'}",
    )


# ---------------------------------------------------------------- smm


def service_text(svc: SmmService) -> str:
    return (
        f"<b>📈 {html.quote(svc.name)}</b>\n"
        f"{svc.description}\n\n"
        f"💳 Цена: <b>{fmt(svc.price_per_1000)}</b> за 1000\n"
        f"🔻 Минимум: <b>{svc.min_qty}</b>\n"
        f"🔺 Максимум: <b>{svc.max_qty}</b>"
    )


@router.callback_query(ServiceCb.filter())
async def show_service(
    call: CallbackQuery,
    callback_data: ServiceCb,
    state: FSMContext,
    session: AsyncSession,
    callback_answer: CallbackAnswer,
) -> None:
    await state.clear()
    svc = await session.get(SmmService, callback_data.id)
    if svc is None:
        callback_answer.text = "Услуга удалена"
        return
    await render(call, service_text(svc), kb.service(svc))


@router.callback_query(BuyServiceCb.filter())
async def ask_smm_quantity(
    call: CallbackQuery,
    callback_data: BuyServiceCb,
    state: FSMContext,
    session: AsyncSession,
    user: User,
    callback_answer: CallbackAnswer,
) -> None:
    svc = await session.get(SmmService, callback_data.id)
    if svc is None:
        callback_answer.text = "Услуга удалена"
        return
    user = await shop.refresh_user(session, user.id)
    affordable = min(svc.max_qty, user.balance * 1000 // svc.price_per_1000) if svc.price_per_1000 else svc.max_qty
    if affordable < svc.min_qty:
        callback_answer.text = (
            f"❗️ Недостаточно средств. Минимальный заказ: {fmt(smm_cost(svc.price_per_1000, svc.min_qty))}"
        )
        callback_answer.show_alert = True
        return
    await state.set_state(SmmStates.quantity)
    await state.update_data(service_id=svc.id)
    await render(
        call,
        f"{service_text(svc)}\n\n💰 Вашего баланса хватит на: <b>{affordable}</b>\n👉 Введите количество:",
        kb.back("main", "🔙 Отмена"),
    )


@router.message(SmmStates.quantity, F.text)
async def smm_quantity(message: Message, state: FSMContext, session: AsyncSession, user: User) -> None:
    assert message.text is not None
    svc = await session.get(SmmService, (await state.get_data())["service_id"])
    if svc is None:
        await state.clear()
        await message.answer("❗️ Услуга удалена.", reply_markup=kb.back())
        return
    text = message.text.strip()
    if not text.isdigit() or not svc.min_qty <= int(text) <= svc.max_qty:
        await message.answer(
            f"❗️ Введите число от {svc.min_qty} до {svc.max_qty}.", reply_markup=kb.back("main", "🔙 Отмена")
        )
        return
    quantity = int(text)
    cost = smm_cost(svc.price_per_1000, quantity)
    user = await shop.refresh_user(session, user.id)
    if cost > user.balance:
        await message.answer(
            f"❗️ Недостаточно средств: нужно {fmt(cost)}, на балансе {fmt(user.balance)}.",
            reply_markup=kb.back("main", "🔙 Отмена"),
        )
        return
    await state.update_data(quantity=quantity)
    await state.set_state(SmmStates.link)
    await message.answer(
        f"♦️ Количество: <b>{quantity}</b>\n💰 Будет списано: <b>{fmt(cost)}</b>\n\n👉 Отправьте ссылку:",
        reply_markup=kb.back("main", "🔙 Отмена"),
    )


@router.message(SmmStates.link, F.text)
async def smm_link(
    message: Message,
    state: FSMContext,
    session: AsyncSession,
    user: User,
    settings: Settings,
    bot: Bot,
    smm: SmmPanelClient | None,
) -> None:
    assert message.text is not None
    data = await state.get_data()
    try:
        order = await shop.buy_smm(session, smm, user.id, data["service_id"], data["quantity"], message.text)
    except OrderNeedsReview as exc:
        await state.clear()
        await message.answer(str(exc), reply_markup=kb.back())
        review = await session.get(Order, exc.order_id)
        if review is not None:
            await alert_admins(bot, settings, review_alert_text(review, "панель не ответила на создание заказа"))
        return
    except ShopError as exc:
        await state.clear()
        await message.answer(str(exc), reply_markup=kb.back())
        return
    await state.clear()
    await message.answer(
        f"✅ <b>Заказ #{order.id} создан</b>\n"
        f"📦 {html.quote(order.title)} × {order.quantity}\n"
        f"🔗 {html.quote(order.link or '')}\n"
        f"💳 Списано: {fmt(order.amount)}\n\n"
        "Статус можно посмотреть в «Профиль → Мои заказы».",
        reply_markup=kb.back("main", "🏠 В меню"),
    )
    await log_event(
        bot,
        settings,
        f"📈 <b>Накрутка #{order.id}</b>\n{html.quote(order.title)} × {order.quantity} = {fmt(order.amount)}\n"
        f"Пользователь: <code>{user.id}</code> @{user.username or '—'}",
    )
