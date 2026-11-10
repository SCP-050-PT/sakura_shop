"""Periodic jobs: auto-credit CryptoBot invoices and sync SMM order statuses."""

import asyncio
import logging
from datetime import datetime, timedelta

from aiogram import Bot
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.bot.handlers.deposit import notify_credit
from app.config import Settings
from app.db.models import Order, OrderStatus, Payment, PaymentStatus, User
from app.money import fmt
from app.services import shop
from app.services.cryptobot import CryptoBotClient
from app.services.smm_api import SmmPanelClient

log = logging.getLogger(__name__)

PAYMENTS_INTERVAL = 30
SMM_INTERVAL = 300


async def poll_cryptobot(
    sessionmaker: async_sessionmaker[AsyncSession], client: CryptoBotClient, bot: Bot, settings: Settings
) -> None:
    async with sessionmaker() as session:
        pending = list(
            await session.scalars(
                select(Payment).where(
                    Payment.provider == "cryptobot",
                    Payment.status == PaymentStatus.PENDING,
                    Payment.external_id.is_not(None),
                )
            )
        )
        if not pending:
            return
        invoices = await client.get_invoices([int(p.external_id) for p in pending if p.external_id])
        # Small grace period: an invoice paid at the last second should still be picked up.
        deadline = datetime.now() - timedelta(minutes=settings.invoice_ttl_minutes + 10)
        for payment in pending:
            invoice = invoices.get(int(payment.external_id or 0))
            if invoice is not None and invoice.status == "paid":
                result = await shop.complete_payment(session, payment.id, settings.referral_percent)
                if result:
                    user = await session.get(User, payment.user_id)
                    await notify_credit(bot, settings, result, user.username if user else None)
            elif (invoice is not None and invoice.status == "expired") or payment.created_at < deadline:
                await shop.expire_payment(session, payment.id)


async def poll_smm(sessionmaker: async_sessionmaker[AsyncSession], client: SmmPanelClient, bot: Bot) -> None:
    async with sessionmaker() as session:
        orders = list(await session.scalars(select(Order).where(Order.status == OrderStatus.PROCESSING).limit(100)))
        for order in orders:
            try:
                updated = await shop.sync_smm_order(session, client, order)
            except Exception:  # noqa: BLE001
                log.warning("SMM status check failed for order %s", order.id)
                continue
            if updated.status in (OrderStatus.FAILED, OrderStatus.PARTIAL) and updated.refunded:
                try:
                    await bot.send_message(
                        updated.user_id,
                        f"↩️ Заказ #{updated.id} выполнен не полностью. На баланс возвращено {fmt(updated.refunded)}.",
                    )
                except Exception:  # noqa: BLE001
                    log.info("Could not notify user %s", updated.user_id)


async def run_background(
    sessionmaker: async_sessionmaker[AsyncSession],
    bot: Bot,
    settings: Settings,
    cryptobot: CryptoBotClient | None,
    smm: SmmPanelClient | None,
) -> None:
    ticks = 0
    while True:
        if cryptobot is not None:
            try:
                await poll_cryptobot(sessionmaker, cryptobot, bot, settings)
            except Exception:
                log.exception("CryptoBot polling failed")
        if smm is not None and ticks % (SMM_INTERVAL // PAYMENTS_INTERVAL) == 0:
            try:
                await poll_smm(sessionmaker, smm, bot)
            except Exception:
                log.exception("SMM polling failed")
        ticks += 1
        await asyncio.sleep(PAYMENTS_INTERVAL)
