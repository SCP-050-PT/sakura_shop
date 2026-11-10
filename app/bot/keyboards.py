from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup, KeyboardButton, ReplyKeyboardMarkup
from aiogram.utils.keyboard import InlineKeyboardBuilder

from app.bot.callbacks import (
    AdminCb,
    BuyProductCb,
    BuyServiceCb,
    CatalogCb,
    CategoryCb,
    CheckPaymentCb,
    Nav,
    OrderCb,
    PayMethodCb,
    ServiceCb,
    SubcategoryCb,
    SupportTopicCb,
)
from app.db.models import Category, CategoryKind, Order, SmmService, Subcategory
from app.money import fmt

MENU_BUTTON_TEXT = "🏠 Меню"

reply_menu = ReplyKeyboardMarkup(keyboard=[[KeyboardButton(text=MENU_BUTTON_TEXT)]], resize_keyboard=True)

SUPPORT_TOPICS = {
    "smm": "📈 Накрутка",
    "product": "📦 Товар",
    "payment": "💳 Оплата",
    "other": "❓ Другое",
}


def btn(text: str, cb: object) -> InlineKeyboardButton:
    return InlineKeyboardButton(text=text, callback_data=cb.pack())  # type: ignore[attr-defined]


def back(to: str = "main", text: str = "🔙 Назад") -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[[btn(text, Nav(to=to))]])


def main_menu(is_admin: bool) -> InlineKeyboardMarkup:
    rows = [
        [
            btn("🛍 Товары", CatalogCb(kind=CategoryKind.PRODUCT)),
            btn("📈 Накрутка", CatalogCb(kind=CategoryKind.SMM)),
        ],
        [btn("👤 Профиль", Nav(to="profile")), btn("💳 Пополнить", Nav(to="deposit"))],
        [btn("🤝 Партнёрка", Nav(to="referral")), btn("🆘 Поддержка", Nav(to="support"))],
        [btn("ℹ️ О магазине", Nav(to="info"))],
    ]
    if is_admin:
        rows.append([btn("👑 Админ-панель", AdminCb(action="panel"))])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def categories(items: list[Category]) -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    for cat in items:
        kb.row(btn(cat.name, CategoryCb(id=cat.id)))
    kb.row(btn("🔙 Назад", Nav(to="main")))
    return kb.as_markup()


def subcategories(items: list[Subcategory], kind: str) -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    for sub in items:
        label = f"{sub.name} — {fmt(sub.price)}" if kind == CategoryKind.PRODUCT else sub.name
        kb.row(btn(label, SubcategoryCb(id=sub.id)))
    kb.row(btn("🔙 Назад", CatalogCb(kind=kind)))
    return kb.as_markup()


def product(sub: Subcategory, in_stock: int) -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    if in_stock:
        kb.row(btn("🛒 Купить", BuyProductCb(sub_id=sub.id)))
    kb.row(btn("🔙 Назад", CategoryCb(id=sub.category_id)))
    return kb.as_markup()


def services(items: list[SmmService], sub: Subcategory) -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    for svc in items:
        kb.row(btn(f"{svc.name} — {fmt(svc.price_per_1000)}/1000", ServiceCb(id=svc.id)))
    kb.row(btn("🔙 Назад", CategoryCb(id=sub.category_id)))
    return kb.as_markup()


def service(svc: SmmService) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [btn("🛒 Заказать", BuyServiceCb(id=svc.id))],
            [btn("🔙 Назад", SubcategoryCb(id=svc.subcategory_id))],
        ]
    )


def profile() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [btn("💳 Пополнить баланс", Nav(to="deposit"))],
            [btn("🧾 Мои заказы", Nav(to="orders")), btn("🎁 Промокод", Nav(to="promo"))],
            [btn("🔙 Назад", Nav(to="main"))],
        ]
    )


ORDER_ICONS = {"completed": "✅", "processing": "⏳", "pending": "⏳", "partial": "🟡", "failed": "❌"}


def orders(items: list[Order]) -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    for order in items:
        icon = ORDER_ICONS.get(order.status, "•")
        kb.row(btn(f"{icon} #{order.id} {order.title} × {order.quantity}", OrderCb(id=order.id)))
    kb.row(btn("🔙 Назад", Nav(to="profile")))
    return kb.as_markup()


def order(item: Order) -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    if item.kind == CategoryKind.PRODUCT and item.delivered:
        kb.row(btn("📦 Показать товар", OrderCb(id=item.id, action="items")))
    if item.kind == CategoryKind.SMM and item.panel_order_id:
        kb.row(btn("🔄 Обновить статус", OrderCb(id=item.id, action="refresh")))
    kb.row(btn("🔙 К заказам", Nav(to="orders")))
    return kb.as_markup()


def pay_methods(cryptobot: bool, stars: bool) -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    if cryptobot:
        kb.row(btn("🪙 CryptoBot (крипта)", PayMethodCb(method="cryptobot")))
    if stars:
        kb.row(btn("⭐️ Telegram Stars", PayMethodCb(method="stars")))
    kb.row(btn("🔙 Отмена", Nav(to="main")))
    return kb.as_markup()


def invoice(url: str, payment_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="💸 Оплатить", url=url)],
            [btn("✅ Я оплатил", CheckPaymentCb(id=payment_id))],
            [btn("🔙 В меню", Nav(to="main"))],
        ]
    )


def support(username: str | None) -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    for key, label in SUPPORT_TOPICS.items():
        kb.button(text=label, callback_data=SupportTopicCb(topic=key))
    kb.adjust(2)
    if username:
        kb.row(InlineKeyboardButton(text="💬 Написать админу", url=f"https://t.me/{username}"))
    kb.row(btn("🔙 Назад", Nav(to="main")))
    return kb.as_markup()
