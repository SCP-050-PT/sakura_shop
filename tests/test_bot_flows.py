"""End-to-end flows through the real Dispatcher with a mocked Telegram API."""

from sqlalchemy import select

from app.bot.callbacks import (
    AdminCatalogCb,
    AdminCb,
    AdminTicketCb,
    AdminUserCb,
    BuyProductCb,
    BuyServiceCb,
    CatalogCb,
    Nav,
    OrderCb,
    PayMethodCb,
    SupportTopicCb,
)
from app.bot.keyboards import MENU_BUTTON_TEXT
from app.db.models import (
    Category,
    Order,
    OrderStatus,
    Payment,
    PaymentStatus,
    SmmService,
    StockItem,
    Subcategory,
    SupportTicket,
    User,
)
from app.services.smm_api import SmmOrderStatus
from tests.conftest import ADMIN_ID
from tests.test_services import FakeSmm


async def test_start_registers_user_and_referral(make_harness, sessionmaker) -> None:  # type: ignore[no-untyped-def]
    h = make_harness()
    await h.message(1, "/start")
    await h.message(2, "/start 1")
    await h.message(2, "/start 3")  # existing user: referrer must not change
    async with sessionmaker() as session:
        assert (await session.get(User, 2)).referrer_id == 1
    assert "новый реферал" in h.api.texts(1)
    assert "Ваш баланс" in h.api.texts(2)


async def test_admin_builds_catalog_and_user_buys(make_harness, sessionmaker) -> None:  # type: ignore[no-untyped-def]
    h = make_harness()
    await h.message(ADMIN_ID, "/start")
    await h.callback(ADMIN_ID, AdminCb(action="catalog"))
    await h.callback(ADMIN_ID, AdminCatalogCb(action="add_cat", arg="product"))
    await h.message(ADMIN_ID, "Аккаунты")
    async with sessionmaker() as session:
        cat = (await session.scalars(select(Category))).one()
    await h.callback(ADMIN_ID, AdminCatalogCb(action="add_sub", id=cat.id))
    await h.message(ADMIN_ID, "Steam\n150\nПолный доступ")
    async with sessionmaker() as session:
        sub = (await session.scalars(select(Subcategory))).one()
        assert sub.price == 15000 and sub.description == "Полный доступ"
    await h.callback(ADMIN_ID, AdminCatalogCb(action="upload", id=sub.id))
    await h.message(ADMIN_ID, "login1:pass1\nlogin2:pass2\nlogin3:pass3")
    # Menu button must leave the upload state instead of becoming stock.
    await h.message(ADMIN_ID, MENU_BUTTON_TEXT)
    await h.message(ADMIN_ID, "not stock")

    # Non-admins cannot reach admin handlers.
    await h.message(7, "/start")
    await h.callback(7, AdminCatalogCb(action="add_cat", arg="product"))
    await h.message(7, "Hacked")

    async with sessionmaker() as session:
        assert len((await session.scalars(select(StockItem))).all()) == 3
        assert len((await session.scalars(select(Category))).all()) == 1

    # Admin credits the buyer via the user card.
    await h.callback(ADMIN_ID, AdminUserCb(action="add", user_id=7))
    await h.message(ADMIN_ID, "400")

    await h.callback(7, CatalogCb(kind="product"))
    await h.callback(7, BuyProductCb(sub_id=sub.id))
    await h.message(7, "abc")
    await h.message(7, "2")
    async with sessionmaker() as session:
        user = await session.get(User, 7)
        order = (await session.scalars(select(Order))).one()
        assert user.balance == 10000
        assert order.amount == 30000 and order.status == OrderStatus.COMPLETED
        assert len((await session.scalars(select(StockItem))).all()) == 1
    delivered = h.api.texts(7)
    assert "login1:pass1" in delivered and "login2:pass2" in delivered

    # Re-showing purchased items from order history works.
    before = len(h.api.requests)
    await h.callback(7, OrderCb(id=order.id, action="items"))
    assert "login1:pass1" in h.api.texts(7) and len(h.api.requests) > before


async def test_stars_deposit_with_referral(make_harness, sessionmaker) -> None:  # type: ignore[no-untyped-def]
    h = make_harness()
    await h.message(1, "/start")
    await h.message(2, "/start 1")
    await h.callback(2, Nav(to="deposit"))
    await h.message(2, "5")  # below minimum
    await h.message(2, "150")
    await h.callback(2, PayMethodCb(method="stars"))
    invoice = h.api.sent("SendInvoice", 2)[-1]
    assert invoice.currency == "XTR" and invoice.prices[0].amount == 100

    await h.pre_checkout(2, invoice.payload, total=99)
    await h.pre_checkout(2, invoice.payload, total=100)
    answers = h.api.sent("AnswerPreCheckoutQuery")
    assert [a.ok for a in answers] == [False, True]

    await h.paid(2, invoice.payload, 100)
    await h.paid(2, invoice.payload, 100, charge_id="dup")  # duplicate update must not double-credit
    async with sessionmaker() as session:
        assert (await session.get(User, 2)).balance == 15000
        assert (await session.get(User, 1)).balance == 1500
        payment = (await session.scalars(select(Payment))).one()
        assert payment.status == PaymentStatus.PAID and payment.external_id == "charge-1"


async def test_smm_order_flow(make_harness, sessionmaker) -> None:  # type: ignore[no-untyped-def]
    smm = FakeSmm()
    h = make_harness(smm=smm)
    await h.message(ADMIN_ID, "/start")
    await h.callback(ADMIN_ID, AdminCatalogCb(action="add_cat", arg="smm"))
    await h.message(ADMIN_ID, "Instagram")
    async with sessionmaker() as session:
        cat = (await session.scalars(select(Category))).one()
    await h.callback(ADMIN_ID, AdminCatalogCb(action="add_sub", id=cat.id))
    await h.message(ADMIN_ID, "Подписчики")
    async with sessionmaker() as session:
        sub = (await session.scalars(select(Subcategory))).one()
    await h.callback(ADMIN_ID, AdminCatalogCb(action="add_svc", id=sub.id))
    await h.message(ADMIN_ID, "Followers HQ\n12\n100\n10\n10000")
    async with sessionmaker() as session:
        svc = (await session.scalars(select(SmmService))).one()
        assert (svc.price_per_1000, svc.min_qty, svc.max_qty) == (10000, 10, 10000)
    await h.callback(ADMIN_ID, AdminCatalogCb(action="edit", id=svc.id, arg="svc.min_qty"))
    await h.message(ADMIN_ID, "50")

    await h.message(5, "/start")
    async with sessionmaker() as session:
        (await session.get(User, 5)).balance = 5000
        await session.commit()
    await h.callback(5, BuyServiceCb(id=svc.id))
    await h.message(5, "20")  # below new minimum
    await h.message(5, "300")
    await h.message(5, "https://instagram.com/sakura")
    assert smm.calls == [("12", "https://instagram.com/sakura", 300)]
    async with sessionmaker() as session:
        order = (await session.scalars(select(Order))).one()
        assert order.status == OrderStatus.PROCESSING and order.amount == 3000
        assert (await session.get(User, 5)).balance == 2000

    smm.status = SmmOrderStatus(status="Canceled", remains=300, charge=None)
    await h.callback(5, OrderCb(id=order.id, action="refresh"))
    async with sessionmaker() as session:
        assert (await session.get(User, 5)).balance == 5000
        assert (await session.get(Order, order.id)).status == OrderStatus.FAILED


async def test_support_ticket_roundtrip(make_harness, sessionmaker) -> None:  # type: ignore[no-untyped-def]
    h = make_harness()
    await h.message(3, "/start")
    await h.callback(3, SupportTopicCb(topic="payment"))
    await h.message(3, "Не пришли деньги <b>")
    await h.callback(3, SupportTopicCb(topic="other"))  # second open ticket is refused
    async with sessionmaker() as session:
        ticket = (await session.scalars(select(SupportTicket))).one()
    assert "&lt;b&gt;" in h.api.texts(ADMIN_ID)

    await h.callback(ADMIN_ID, AdminTicketCb(action="answer", id=ticket.id))
    await h.message(ADMIN_ID, "Проверили, зачислено")
    async with sessionmaker() as session:
        ticket = await session.get(SupportTicket, ticket.id)
        assert ticket.status == "closed" and ticket.answer == "Проверили, зачислено"
    assert "Проверили, зачислено" in h.api.texts(3)


class FakeCryptoBot:
    def __init__(self) -> None:
        self.statuses: dict[int, str] = {}

    async def create_invoice(self, amount_kopecks: int, *, description: str, payload: str, expires_in: int):  # type: ignore[no-untyped-def]
        from app.services.cryptobot import CryptoInvoice

        invoice_id = 100 + len(self.statuses)
        self.statuses[invoice_id] = "active"
        return CryptoInvoice(invoice_id, "active", f"https://t.me/CryptoBot?start=IV{invoice_id}")

    async def get_invoices(self, invoice_ids: list[int]):  # type: ignore[no-untyped-def]
        from app.services.cryptobot import CryptoInvoice

        return {i: CryptoInvoice(i, self.statuses[i], "u") for i in invoice_ids if i in self.statuses}


async def test_cryptobot_check_button_and_background_poll(make_harness, sessionmaker, settings) -> None:  # type: ignore[no-untyped-def]
    from app.background import poll_cryptobot

    crypto = FakeCryptoBot()
    h = make_harness(cryptobot=crypto)
    await h.message(4, "/start")

    # Invoice #1: paid, credited via the "Я оплатил" button.
    await h.callback(4, Nav(to="deposit"))
    await h.message(4, "200")
    await h.callback(4, PayMethodCb(method="cryptobot"))
    async with sessionmaker() as session:
        first = (await session.scalars(select(Payment))).one()
    from app.bot.callbacks import CheckPaymentCb

    await h.callback(4, CheckPaymentCb(id=first.id))
    async with sessionmaker() as session:
        assert (await session.get(User, 4)).balance == 0
    crypto.statuses[int(first.external_id)] = "paid"
    await h.callback(4, CheckPaymentCb(id=first.id))
    await h.callback(4, CheckPaymentCb(id=first.id))

    # Invoice #2: paid, picked up by the background poller without user action.
    await h.callback(4, Nav(to="deposit"))
    await h.message(4, "50")
    await h.callback(4, PayMethodCb(method="cryptobot"))
    async with sessionmaker() as session:
        second = (await session.scalars(select(Payment).where(Payment.id != first.id))).one()
    crypto.statuses[int(second.external_id)] = "paid"
    await poll_cryptobot(sessionmaker, crypto, h.bot, settings)  # type: ignore[arg-type]
    await poll_cryptobot(sessionmaker, crypto, h.bot, settings)  # type: ignore[arg-type]

    async with sessionmaker() as session:
        assert (await session.get(User, 4)).balance == 25000
        assert (await session.get(User, 4)).total_deposited == 25000
    assert h.api.texts(4).count("Платёж получен") == 2
