from aiogram import Bot, html

from app.db.models import Order, StockKind

MESSAGE_LIMIT = 3800


async def deliver_order(bot: Bot, chat_id: int, order: Order) -> None:
    """Send purchased stock: text items are batched by whole items, files are sent by file_id."""
    items = order.delivered or []
    part = f"📦 <b>Ваш товар (заказ #{order.id}):</b>"
    for item in items:
        if item["kind"] != StockKind.TEXT:
            continue
        # Telegram limit is 4096 chars; very long single items get truncated rather than split mid-tag.
        entry = f"\n\n<code>{html.quote(item['content'][: MESSAGE_LIMIT - 100])}</code>"
        if len(part) + len(entry) > MESSAGE_LIMIT:
            await bot.send_message(chat_id, part)
            part = ""
        part += entry
    if any(i["kind"] == StockKind.TEXT for i in items):
        await bot.send_message(chat_id, part.lstrip())
    for item in items:
        if item["kind"] == StockKind.FILE:
            await bot.send_document(chat_id, item["content"], caption=f"📦 Заказ #{order.id}")
