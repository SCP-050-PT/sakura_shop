"""Regression tests for review fixes: inline mode, stuck SMM orders, Stars refunds, migrations, UTC times."""

import time
from datetime import timedelta

import pytest
from aiogram.types import InlineQuery
from alembic.autogenerate import compare_metadata
from alembic.migration import MigrationContext
from sqlalchemy import select, text

from app.background import flag_stuck_orders, poll_cryptobot
from app.bot.callbacks import BuyServiceCb, Nav, PayMethodCb
from app.db import create_engine, init_db, migrate
from app.db.models import (
    Base,
    Category,
    CategoryKind,
    Order,
    OrderStatus,
    Payment,
    PaymentStatus,
    StockItem,
    Subcategory,
    User,
    utcnow,
)
from app.services import shop
from app.services.smm_api import SmmTransportError
from tests.conftest import ADMIN_ID
from tests.test_bot_flows import FakeCryptoBot
from tests.test_services import FakeSmm, _service, _user


class TimeoutSmm(FakeSmm):
    async def add_order(self, service_id: str, link: str, quantity: int) -> str:
        self.calls.append((service_id, link, quantity))
        raise SmmTransportError("read timeout")


async def _order_smm_with_timeout(h, sessionmaker) -> Order:  # type: ignore[no-untyped-def]
    async with sessionmaker() as session:
        svc = await _service(session)
        await _user(session, 5, balance=5000)
    await h.message(5, "/start")
    await h.callback(5, BuyServiceCb(id=svc.id))
    await h.message(5, "300")
    await h.message(5, "https://instagram.com/sakura")
    async with sessionmaker() as session:
        return (await session.scalars(select(Order))).one()


async def test_smm_timeout_holds_money_for_admin_review(make_harness, sessionmaker) -> None:  # type: ignore[no-untyped-def]
    h = make_harness(smm=TimeoutSmm())
    order = await _order_smm_with_timeout(h, sessionmaker)
    assert order.status == OrderStatus.REVIEW and order.refunded == 0
    async with sessionmaker() as session:
        assert (await session.get(User, 5)).balance == 2000
    assert "передан администратору" in h.api.texts(5)
    assert f"/order_refund {order.id}" in h.api.texts(ADMIN_ID)

    await h.message(ADMIN_ID, "/review")
    assert f"#{order.id}" in h.api.texts(ADMIN_ID)
    await h.message(ADMIN_ID, f"/order_refund {order.id}")
    await h.message(ADMIN_ID, f"/order_refund {order.id}")  # second refund must be refused
    async with sessionmaker() as session:
        assert (await session.get(User, 5)).balance == 5000
        refunded = await session.get(Order, order.id)
        assert refunded.status == OrderStatus.FAILED and refunded.refunded == 3000
    assert "не на проверке" in h.api.texts(ADMIN_ID)


async def test_admin_attaches_review_order_to_panel(make_harness, sessionmaker) -> None:  # type: ignore[no-untyped-def]
    h = make_harness(smm=TimeoutSmm())
    order = await _order_smm_with_timeout(h, sessionmaker)
    await h.message(ADMIN_ID, f"/order_attach {order.id} 98765")
    async with sessionmaker() as session:
        attached = await session.get(Order, order.id)
        assert attached.status == OrderStatus.PROCESSING and attached.panel_order_id == "98765"
        assert (await session.get(User, 5)).balance == 2000


async def test_orders_stuck_before_panel_are_flagged(make_harness, sessionmaker, settings) -> None:  # type: ignore[no-untyped-def]
    h = make_harness()
    async with sessionmaker() as session:
        await _user(session, 6)
        common = {"user_id": 6, "kind": CategoryKind.SMM, "title": "Likes", "quantity": 100, "amount": 500}
        session.add_all(
            [
                Order(**common, refunded=0, status=OrderStatus.PENDING, created_at=utcnow() - timedelta(hours=1)),
                Order(**common, refunded=0, status=OrderStatus.PENDING),  # may still be talking to the panel
            ]
        )
        await session.commit()
    await flag_stuck_orders(sessionmaker, h.bot, settings)
    async with sessionmaker() as session:
        statuses = [o.status for o in (await session.scalars(select(Order).order_by(Order.id))).all()]
    assert statuses == [OrderStatus.REVIEW, OrderStatus.PENDING]
    assert "не дошёл до панели" in h.api.texts(ADMIN_ID)


async def test_inline_search_and_product_deep_link(make_harness, sessionmaker) -> None:  # type: ignore[no-untyped-def]
    h = make_harness()
    async with sessionmaker() as session:
        await _user(session, 7)
        cat = Category(kind=CategoryKind.PRODUCT, name="Игры")
        session.add(cat)
        await session.flush()
        steam = Subcategory(category_id=cat.id, name="Стим аккаунт", price=15000)
        empty = Subcategory(category_id=cat.id, name="Стим ключ", price=500)
        session.add_all([steam, empty])
        await session.flush()
        session.add_all(StockItem(subcategory_id=steam.id, content=c) for c in ("a", "b"))
        await session.commit()

    await h.feed(inline_query=InlineQuery(id="q1", from_user=h.tg_user(7), query="СТИМ", offset=""))
    answer = h.api.sent("AnswerInlineQuery")[-1]
    # Cyrillic matched case-insensitively; the out-of-stock product is hidden.
    assert [r.id for r in answer.results] == [str(steam.id)]
    assert "В наличии: <b>2</b>" in answer.results[0].input_message_content.message_text
    url = answer.results[0].reply_markup.inline_keyboard[0][0].url
    assert url == f"https://t.me/sakura_test_bot?start=p{steam.id}_7"

    await h.message(8, f"/start p{steam.id}_7")
    async with sessionmaker() as session:
        assert (await session.get(User, 8)).referrer_id == 7
    assert "Стим аккаунт" in h.api.texts(8)


async def _stars_deposit(h, user_id: int, rub: str) -> int:  # type: ignore[no-untyped-def]
    await h.message(user_id, "/start")
    await h.callback(user_id, Nav(to="deposit"))
    await h.message(user_id, rub)
    await h.callback(user_id, PayMethodCb(method="stars"))
    invoice = h.api.sent("SendInvoice", user_id)[-1]
    total = invoice.prices[0].amount
    await h.pre_checkout(user_id, invoice.payload, total=total)
    await h.paid(user_id, invoice.payload, total, charge_id=f"charge-{user_id}")
    return int(invoice.payload.split(":")[1])


async def test_admin_refunds_stars_payment(make_harness, sessionmaker) -> None:  # type: ignore[no-untyped-def]
    h = make_harness()
    payment_id = await _stars_deposit(h, 9, "150")
    await h.message(ADMIN_ID, f"/refund_stars {payment_id}")
    await h.message(ADMIN_ID, f"/refund_stars {payment_id}")  # already refunded
    refunds = h.api.sent("RefundStarPayment")
    assert len(refunds) == 1 and refunds[0].telegram_payment_charge_id == "charge-9"
    async with sessionmaker() as session:
        user = await session.get(User, 9)
        assert user.balance == 0 and user.total_deposited == 0
        assert (await session.get(Payment, payment_id)).status == PaymentStatus.REFUNDED


async def test_stars_refund_refused_when_balance_already_spent(make_harness, sessionmaker) -> None:  # type: ignore[no-untyped-def]
    h = make_harness()
    payment_id = await _stars_deposit(h, 10, "150")
    async with sessionmaker() as session:
        assert await shop.change_balance(session, 10, -10000)
        await session.commit()
    await h.message(ADMIN_ID, f"/refund_stars {payment_id}")
    assert h.api.sent("RefundStarPayment") == []
    async with sessionmaker() as session:
        assert (await session.get(User, 10)).balance == 5000
        assert (await session.get(Payment, payment_id)).status == PaymentStatus.PAID


async def test_fresh_invoice_survives_poll_in_non_utc_timezone(
    make_harness, sessionmaker, settings, monkeypatch
) -> None:  # type: ignore[no-untyped-def]
    # Moscow/Asia servers used to expire unpaid invoices on the first poll: local "now" was
    # compared with UTC timestamps, so every invoice looked hours old.
    monkeypatch.setenv("TZ", "Asia/Tokyo")
    time.tzset()
    try:
        crypto = FakeCryptoBot()
        h = make_harness(cryptobot=crypto)
        await h.message(11, "/start")
        await h.callback(11, Nav(to="deposit"))
        await h.message(11, "200")
        await h.callback(11, PayMethodCb(method="cryptobot"))
        await poll_cryptobot(sessionmaker, crypto, h.bot, settings)  # type: ignore[arg-type]
        async with sessionmaker() as session:
            assert (await session.scalars(select(Payment))).one().status == PaymentStatus.PENDING
    finally:
        monkeypatch.delenv("TZ")
        time.tzset()


def _schema_diff(sync_conn):  # type: ignore[no-untyped-def]
    return compare_metadata(MigrationContext.configure(sync_conn), Base.metadata)


async def test_migrations_match_models(settings) -> None:  # type: ignore[no-untyped-def]
    engine = create_engine(settings.database_url)
    try:
        await migrate(engine, settings.database_url)
        async with engine.connect() as conn:
            assert await conn.run_sync(_schema_diff) == []
    finally:
        await engine.dispose()


async def test_migrate_adopts_database_created_without_alembic(settings) -> None:  # type: ignore[no-untyped-def]
    engine = create_engine(settings.database_url)
    try:
        await init_db(engine)  # what v1.0 did on startup
        async with engine.begin() as conn:
            await conn.execute(
                text(
                    "INSERT INTO users (id, balance, total_deposited, referral_earned, "
                    "deposit_bonus_percent, is_banned) VALUES (1, 700, 0, 0, 0, 0)"
                )
            )
        await migrate(engine, settings.database_url)
        async with engine.connect() as conn:
            assert (await conn.execute(text("SELECT version_num FROM alembic_version"))).scalar() == "0001"
            assert (await conn.execute(text("SELECT balance FROM users WHERE id = 1"))).scalar() == 700
    finally:
        await engine.dispose()


@pytest.mark.parametrize("args", ["p12_34", "p12"])
def test_product_start_param_roundtrip(args: str) -> None:
    from app.bot.handlers.inline import parse_product_start

    parsed = parse_product_start(args)
    assert parsed is not None and parsed[0] == 12
    assert parse_product_start("12") is None and parse_product_start(None) is None
