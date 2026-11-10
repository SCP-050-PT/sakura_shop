"""Inline mode: `@bot <запрос>` in any chat searches in-stock products and shares a card."""

import re

from aiogram import Bot, Router
from aiogram.types import (
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    InlineQuery,
    InlineQueryResultArticle,
    InlineQueryResultsButton,
    InputTextMessageContent,
)
from sqlalchemy.ext.asyncio import AsyncSession

from app.bot.handlers.catalog import product_text
from app.config import Settings
from app.money import fmt
from app.services import shop

router = Router(name="inline")

PAGE_SIZE = 20  # Telegram allows up to 50 results per answer.
CACHE_SECONDS = 30
# Deep-link payload: p<product id>[_<id of the user who shared it>]
PRODUCT_LINK = re.compile(r"^p(\d+)(?:_(\d+))?$")


def product_start_param(sub_id: int, sharer_id: int) -> str:
    return f"p{sub_id}_{sharer_id}"


def parse_product_start(args: str | None) -> tuple[int, int | None] | None:
    match = PRODUCT_LINK.match(args or "")
    if match is None:
        return None
    return int(match[1]), int(match[2]) if match[2] else None


@router.inline_query()
async def inline_search(query: InlineQuery, session: AsyncSession, settings: Settings, bot: Bot) -> None:
    me = await bot.me()
    offset = int(query.offset) if query.offset.isdigit() else 0
    found = await shop.search_products(session, query.query)
    page = found[offset : offset + PAGE_SIZE]
    results = [
        InlineQueryResultArticle(
            id=str(sub.id),
            title=f"{sub.name} — {fmt(sub.price)}",
            description=f"В наличии: {count} шт. {sub.description}"[:200],
            input_message_content=InputTextMessageContent(message_text=product_text(sub, count)),
            reply_markup=InlineKeyboardMarkup(
                inline_keyboard=[
                    [
                        InlineKeyboardButton(
                            text="🛒 Купить в боте",
                            url=f"https://t.me/{me.username}?start={product_start_param(sub.id, query.from_user.id)}",
                        )
                    ]
                ]
            ),
        )
        for sub, count in page
    ]
    next_offset = str(offset + PAGE_SIZE) if offset + PAGE_SIZE < len(found) else ""
    await query.answer(
        results,
        cache_time=CACHE_SECONDS,
        # Links carry the sharer's referral id, so results differ per user.
        is_personal=True,
        next_offset=next_offset,
        button=InlineQueryResultsButton(text=f"🌸 Открыть {settings.shop_name}", start_parameter="inline"),
    )
