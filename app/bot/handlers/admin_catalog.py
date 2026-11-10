from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from aiogram import F, Router, html
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, InlineKeyboardMarkup, Message
from aiogram.utils.callback_answer import CallbackAnswer
from aiogram.utils.keyboard import InlineKeyboardBuilder
from sqlalchemy import delete
from sqlalchemy.ext.asyncio import AsyncSession

from app.bot.callbacks import AdminCatalogCb, AdminCb
from app.bot.filters import IsAdmin
from app.bot.keyboards import btn
from app.bot.states import AdminStates
from app.bot.ui import render
from app.db.models import Category, CategoryKind, SmmService, StockItem, StockKind, Subcategory
from app.money import fmt, parse_rub
from app.services import shop

router = Router(name="admin_catalog")
router.message.filter(IsAdmin())
router.callback_query.filter(IsAdmin())

KIND_TITLES = {CategoryKind.PRODUCT: "📦 Товары", CategoryKind.SMM: "📈 Накрутка"}
STOCK_SEPARATOR = "---"


def ac(action: str, id: int = 0, arg: str = "") -> AdminCatalogCb:  # noqa: A002
    return AdminCatalogCb(action=action, id=id, arg=arg)


def back(action: str, id: int = 0, arg: str = "") -> InlineKeyboardMarkup:  # noqa: A002
    return InlineKeyboardMarkup(inline_keyboard=[[btn("🔙 Назад", ac(action, id, arg))]])


# ---------------------------------------------------------------- screens


@router.callback_query(AdminCb.filter(F.action == "catalog"))
@router.callback_query(AdminCatalogCb.filter(F.action == "root"))
async def root(call: CallbackQuery, state: FSMContext) -> None:
    await state.clear()
    kb = InlineKeyboardBuilder()
    for kind, title in KIND_TITLES.items():
        kb.row(btn(title, ac("kind", arg=kind)))
    kb.row(btn("🔙 Назад", AdminCb(action="panel")))
    await render(call, "<b>🗂 Каталог</b>\n\nВыберите раздел:", kb.as_markup())


async def kind_screen(target: Message | CallbackQuery, session: AsyncSession, kind: str) -> None:
    cats = await shop.list_categories(session, CategoryKind(kind))
    kb = InlineKeyboardBuilder()
    for cat in cats:
        kb.row(btn(f"📁 {cat.name}", ac("cat", cat.id)))
    kb.row(btn("➕ Категория", ac("add_cat", arg=kind)))
    kb.row(btn("🔙 Назад", ac("root")))
    await render(target, f"<b>{KIND_TITLES[CategoryKind(kind)]}</b> — категории:", kb.as_markup())


@router.callback_query(AdminCatalogCb.filter(F.action == "kind"))
async def kind_view(
    call: CallbackQuery, callback_data: AdminCatalogCb, state: FSMContext, session: AsyncSession
) -> None:
    await state.clear()
    await kind_screen(call, session, callback_data.arg)


async def category_screen(target: Message | CallbackQuery, session: AsyncSession, cat_id: int) -> None:
    cat = await session.get(Category, cat_id, populate_existing=True)
    if cat is None:
        await render(target, "❗️ Категория удалена.", back("root"))
        return
    kb = InlineKeyboardBuilder()
    for sub in await shop.list_subcategories(session, cat.id):
        kb.row(btn(f"📂 {sub.name}", ac("sub", sub.id)))
    kb.row(btn("➕ Подкатегория", ac("add_sub", cat.id)))
    kb.row(btn("✏️ Название", ac("edit", cat.id, "cat.name")), btn("🗑 Удалить", ac("del", cat.id, "cat")))
    kb.row(btn("🔙 Назад", ac("kind", arg=cat.kind)))
    await render(target, f"<b>📁 {html.quote(cat.name)}</b>\nПодкатегории:", kb.as_markup())


@router.callback_query(AdminCatalogCb.filter(F.action == "cat"))
async def category_view(
    call: CallbackQuery, callback_data: AdminCatalogCb, state: FSMContext, session: AsyncSession
) -> None:
    await state.clear()
    await category_screen(call, session, callback_data.id)


async def subcategory_screen(target: Message | CallbackQuery, session: AsyncSession, sub_id: int) -> None:
    sub = await session.get(Subcategory, sub_id, populate_existing=True)
    if sub is None:
        await render(target, "❗️ Подкатегория удалена.", back("root"))
        return
    cat = await session.get(Category, sub.category_id)
    assert cat is not None
    kb = InlineKeyboardBuilder()
    lines = [f"<b>📂 {html.quote(sub.name)}</b>", f"Описание: {sub.description or '—'}"]
    if cat.kind == CategoryKind.PRODUCT:
        lines += [f"Цена: <b>{fmt(sub.price)}</b>", f"На складе: <b>{await shop.stock_count(session, sub.id)}</b> шт."]
        kb.row(btn("📥 Загрузить товар", ac("upload", sub.id)), btn("🧹 Очистить склад", ac("clear", sub.id)))
        kb.row(btn("✏️ Цена", ac("edit", sub.id, "sub.price")))
    else:
        for svc in await shop.list_services(session, sub.id):
            kb.row(btn(f"⚙️ {svc.name} — {fmt(svc.price_per_1000)}/1000", ac("svc", svc.id)))
        kb.row(btn("➕ Услуга", ac("add_svc", sub.id)))
    kb.row(btn("✏️ Название", ac("edit", sub.id, "sub.name")), btn("✏️ Описание", ac("edit", sub.id, "sub.description")))
    kb.row(btn("🗑 Удалить", ac("del", sub.id, "sub")), btn("🔙 Назад", ac("cat", cat.id)))
    await render(target, "\n".join(lines), kb.as_markup())


@router.callback_query(AdminCatalogCb.filter(F.action == "sub"))
async def subcategory_view(
    call: CallbackQuery, callback_data: AdminCatalogCb, state: FSMContext, session: AsyncSession
) -> None:
    await state.clear()
    await subcategory_screen(call, session, callback_data.id)


async def service_screen(target: Message | CallbackQuery, session: AsyncSession, svc_id: int) -> None:
    svc = await session.get(SmmService, svc_id, populate_existing=True)
    if svc is None:
        await render(target, "❗️ Услуга удалена.", back("root"))
        return
    kb = InlineKeyboardBuilder()
    kb.row(btn("✏️ Название", ac("edit", svc.id, "svc.name")), btn("✏️ Описание", ac("edit", svc.id, "svc.description")))
    kb.row(
        btn("✏️ Цена/1000", ac("edit", svc.id, "svc.price_per_1000")),
        btn("✏️ ID в панели", ac("edit", svc.id, "svc.panel_service_id")),
    )
    kb.row(btn("✏️ Минимум", ac("edit", svc.id, "svc.min_qty")), btn("✏️ Максимум", ac("edit", svc.id, "svc.max_qty")))
    kb.row(btn("🗑 Удалить", ac("del", svc.id, "svc")), btn("🔙 Назад", ac("sub", svc.subcategory_id)))
    await render(
        target,
        f"<b>⚙️ {html.quote(svc.name)}</b>\n"
        f"Описание: {svc.description or '—'}\n"
        f"ID услуги в панели: <code>{html.quote(svc.panel_service_id)}</code>\n"
        f"Цена за 1000: <b>{fmt(svc.price_per_1000)}</b>\n"
        f"Мин/макс: {svc.min_qty} / {svc.max_qty}",
        kb.as_markup(),
    )


@router.callback_query(AdminCatalogCb.filter(F.action == "svc"))
async def service_view(
    call: CallbackQuery, callback_data: AdminCatalogCb, state: FSMContext, session: AsyncSession
) -> None:
    await state.clear()
    await service_screen(call, session, callback_data.id)


# ---------------------------------------------------------------- create


@router.callback_query(AdminCatalogCb.filter(F.action == "add_cat"))
async def add_category_ask(call: CallbackQuery, callback_data: AdminCatalogCb, state: FSMContext) -> None:
    await state.set_state(AdminStates.new_category)
    await state.update_data(kind=callback_data.arg)
    await render(call, "Отправьте название новой категории:", back("kind", arg=callback_data.arg))


@router.message(AdminStates.new_category, F.text)
async def add_category(message: Message, state: FSMContext, session: AsyncSession) -> None:
    assert message.text is not None
    kind = (await state.get_data())["kind"]
    session.add(Category(kind=kind, name=message.text.strip()[:128]))
    await session.commit()
    await state.clear()
    await kind_screen(message, session, kind)


@router.callback_query(AdminCatalogCb.filter(F.action == "add_sub"))
async def add_subcategory_ask(
    call: CallbackQuery, callback_data: AdminCatalogCb, state: FSMContext, session: AsyncSession
) -> None:
    cat = await session.get(Category, callback_data.id)
    if cat is None:
        return
    await state.set_state(AdminStates.new_subcategory)
    await state.update_data(cat_id=cat.id)
    if cat.kind == CategoryKind.PRODUCT:
        hint = (
            "Отправьте подкатегорию (товар) в формате:\n\n"
            "<code>Название\nЦена в рублях\nОписание (необязательно, можно в несколько строк)</code>"
        )
    else:
        hint = "Отправьте подкатегорию в формате:\n\n<code>Название\nОписание (необязательно)</code>"
    await render(call, hint, back("cat", cat.id))


@router.message(AdminStates.new_subcategory, F.text)
async def add_subcategory(message: Message, state: FSMContext, session: AsyncSession) -> None:
    assert message.text is not None
    cat = await session.get(Category, (await state.get_data())["cat_id"])
    if cat is None:
        await state.clear()
        return
    lines = message.text.strip().split("\n")
    html_lines = message.html_text.strip().split("\n")
    name = lines[0].strip()[:128]
    price = 0
    desc_from = 1
    if cat.kind == CategoryKind.PRODUCT:
        parsed = parse_rub(lines[1]) if len(lines) > 1 else None
        if parsed is None:
            await message.answer(
                "❗️ Во второй строке должна быть цена, например <code>150</code>.", reply_markup=back("cat", cat.id)
            )
            return
        price, desc_from = parsed, 2
    sub = Subcategory(category_id=cat.id, name=name, price=price, description="\n".join(html_lines[desc_from:]).strip())
    session.add(sub)
    await session.commit()
    await state.clear()
    await subcategory_screen(message, session, sub.id)


@router.callback_query(AdminCatalogCb.filter(F.action == "add_svc"))
async def add_service_ask(call: CallbackQuery, callback_data: AdminCatalogCb, state: FSMContext) -> None:
    await state.set_state(AdminStates.new_service)
    await state.update_data(sub_id=callback_data.id)
    await render(
        call,
        "Отправьте услугу в формате:\n\n"
        "<code>Название\nID услуги в SMM-панели\nЦена за 1000 в рублях\n"
        "Минимум\nМаксимум\nОписание (необязательно)</code>\n\n"
        "Пример:\n<code>Подписчики Instagram\n1234\n150\n100\n50000\nБез списаний, старт 0–2 ч.</code>",
        back("sub", callback_data.id),
    )


@router.message(AdminStates.new_service, F.text)
async def add_service(message: Message, state: FSMContext, session: AsyncSession) -> None:
    assert message.text is not None
    sub_id = (await state.get_data())["sub_id"]
    lines = [line.strip() for line in message.text.strip().split("\n")]
    html_lines = message.html_text.strip().split("\n")
    price = parse_rub(lines[2]) if len(lines) > 2 else None
    ok = len(lines) >= 5 and price is not None and lines[3].isdigit() and lines[4].isdigit()
    if not ok or int(lines[3]) < 1 or int(lines[3]) > int(lines[4]):
        await message.answer(
            "❗️ Неверный формат, проверьте строки и попробуйте ещё раз.", reply_markup=back("sub", sub_id)
        )
        return
    assert price is not None
    svc = SmmService(
        subcategory_id=sub_id,
        name=lines[0][:128],
        panel_service_id=lines[1][:32],
        price_per_1000=price,
        min_qty=int(lines[3]),
        max_qty=int(lines[4]),
        description="\n".join(html_lines[5:]).strip(),
    )
    session.add(svc)
    await session.commit()
    await state.clear()
    await service_screen(message, session, svc.id)


# ---------------------------------------------------------------- edit


@dataclass(frozen=True, slots=True)
class Field:
    model: type
    attr: str
    title: str
    parse: Callable[[Message], Any]


def _text(message: Message) -> str | None:
    return (message.text or "").strip()[:128] or None


def _html(message: Message) -> str | None:
    value = (message.html_text or "").strip()
    return "" if value == "-" else value


def _money(message: Message) -> int | None:
    return parse_rub(message.text or "")


def _int(message: Message) -> int | None:
    value = (message.text or "").strip()
    return int(value) if value.isdigit() and int(value) > 0 else None


FIELDS: dict[str, Field] = {
    "cat.name": Field(Category, "name", "название", _text),
    "sub.name": Field(Subcategory, "name", "название", _text),
    "sub.description": Field(Subcategory, "description", "описание («-» чтобы очистить)", _html),
    "sub.price": Field(Subcategory, "price", "цену в рублях", _money),
    "svc.name": Field(SmmService, "name", "название", _text),
    "svc.description": Field(SmmService, "description", "описание («-» чтобы очистить)", _html),
    "svc.price_per_1000": Field(SmmService, "price_per_1000", "цену за 1000 в рублях", _money),
    "svc.panel_service_id": Field(SmmService, "panel_service_id", "ID услуги в панели", _text),
    "svc.min_qty": Field(SmmService, "min_qty", "минимальное количество", _int),
    "svc.max_qty": Field(SmmService, "max_qty", "максимальное количество", _int),
}


async def show_owner(target: Message | CallbackQuery, session: AsyncSession, field_key: str, obj_id: int) -> None:
    prefix = field_key.split(".")[0]
    screens = {"cat": category_screen, "sub": subcategory_screen, "svc": service_screen}
    await screens[prefix](target, session, obj_id)


@router.callback_query(AdminCatalogCb.filter(F.action == "edit"))
async def edit_ask(call: CallbackQuery, callback_data: AdminCatalogCb, state: FSMContext) -> None:
    field = FIELDS.get(callback_data.arg)
    if field is None:
        return
    await state.set_state(AdminStates.edit_field)
    await state.update_data(field=callback_data.arg, obj_id=callback_data.id)
    prefix = callback_data.arg.split(".")[0]
    await render(call, f"✏️ Отправьте новое {field.title}:", back(prefix, callback_data.id))


@router.message(AdminStates.edit_field, F.text)
async def edit_save(message: Message, state: FSMContext, session: AsyncSession) -> None:
    data = await state.get_data()
    field = FIELDS[data["field"]]
    value = field.parse(message)
    if value is None:
        await message.answer("❗️ Некорректное значение, попробуйте ещё раз.")
        return
    obj = await session.get(field.model, data["obj_id"])
    if obj is not None:
        if isinstance(obj, SmmService) and field.attr in ("min_qty", "max_qty"):
            low = value if field.attr == "min_qty" else obj.min_qty
            high = value if field.attr == "max_qty" else obj.max_qty
            if low > high:
                await message.answer("❗️ Минимум не может быть больше максимума.")
                return
        setattr(obj, field.attr, value)
        await session.commit()
    await state.clear()
    await show_owner(message, session, data["field"], data["obj_id"])


# ---------------------------------------------------------------- delete


@router.callback_query(AdminCatalogCb.filter(F.action == "del"))
async def delete_confirm(call: CallbackQuery, callback_data: AdminCatalogCb) -> None:
    kb = InlineKeyboardBuilder()
    kb.row(
        btn("🗑 Да, удалить", ac("del_ok", callback_data.id, callback_data.arg)),
        btn("Отмена", ac(callback_data.arg, callback_data.id)),
    )
    warn = "\n\nВсё вложенное (подкатегории, услуги, склад) тоже удалится." if callback_data.arg != "svc" else ""
    await render(call, f"Удалить безвозвратно?{warn}", kb.as_markup())


@router.callback_query(AdminCatalogCb.filter(F.action == "del_ok"))
async def delete_do(call: CallbackQuery, callback_data: AdminCatalogCb, session: AsyncSession) -> None:
    models = {"cat": Category, "sub": Subcategory, "svc": SmmService}
    obj = await session.get(models[callback_data.arg], callback_data.id)
    if obj is None:
        await render(call, "Уже удалено.", back("root"))
        return
    if isinstance(obj, Category):
        parent: tuple[str, int, str] = ("kind", 0, obj.kind)
    elif isinstance(obj, Subcategory):
        parent = ("cat", obj.category_id, "")
    else:
        parent = ("sub", obj.subcategory_id, "")
    await session.delete(obj)
    await session.commit()
    await render(call, "✅ Удалено.", back(*parent))


# ---------------------------------------------------------------- stock


@router.callback_query(AdminCatalogCb.filter(F.action == "upload"))
async def upload_ask(call: CallbackQuery, callback_data: AdminCatalogCb, state: FSMContext) -> None:
    await state.set_state(AdminStates.upload_stock)
    await state.update_data(sub_id=callback_data.id)
    kb = InlineKeyboardBuilder()
    kb.row(btn("✅ Готово", ac("sub", callback_data.id)))
    await render(
        call,
        "📥 <b>Загрузка товара</b>\n\n"
        "• Текст: каждая строка — отдельная единица товара.\n"
        f"• Если товар многострочный, разделяйте единицы строкой <code>{STOCK_SEPARATOR}</code>.\n"
        "• Файлы: отправьте документы — каждый файл станет единицей товара.\n\n"
        "Можно отправить несколько сообщений подряд, затем нажмите «Готово».",
        kb.as_markup(),
    )


def split_stock(text: str) -> list[str]:
    lines = text.strip().split("\n")
    if any(line.strip() == STOCK_SEPARATOR for line in lines):
        blocks, current = [], []
        for line in lines:
            if line.strip() == STOCK_SEPARATOR:
                blocks.append("\n".join(current).strip())
                current = []
            else:
                current.append(line)
        blocks.append("\n".join(current).strip())
        return [b for b in blocks if b]
    return [line.strip() for line in lines if line.strip()]


@router.message(AdminStates.upload_stock, F.text | F.document)
async def upload_stock(message: Message, state: FSMContext, session: AsyncSession) -> None:
    sub_id = (await state.get_data())["sub_id"]
    if await session.get(Subcategory, sub_id) is None:
        await state.clear()
        return
    if message.document:
        items = [
            StockItem(
                subcategory_id=sub_id,
                kind=StockKind.FILE,
                content=message.document.file_id,
                file_name=message.document.file_name,
            )
        ]
    else:
        items = [
            StockItem(subcategory_id=sub_id, kind=StockKind.TEXT, content=c) for c in split_stock(message.text or "")
        ]
    session.add_all(items)
    await session.commit()
    total = await shop.stock_count(session, sub_id)
    kb = InlineKeyboardBuilder()
    kb.row(btn("✅ Готово", ac("sub", sub_id)))
    await message.answer(f"➕ Добавлено: {len(items)}. На складе: {total}.", reply_markup=kb.as_markup())


@router.callback_query(AdminCatalogCb.filter(F.action == "clear"))
async def clear_stock(
    call: CallbackQuery, callback_data: AdminCatalogCb, session: AsyncSession, callback_answer: CallbackAnswer
) -> None:
    result = await session.execute(delete(StockItem).where(StockItem.subcategory_id == callback_data.id))
    await session.commit()
    callback_answer.text = f"Удалено единиц: {result.rowcount}"
    await subcategory_screen(call, session, callback_data.id)
