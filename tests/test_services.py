from decimal import Decimal

import httpx
import pytest
from sqlalchemy import select

from app.db.models import Category, CategoryKind, Order, OrderStatus, SmmService, StockItem, Subcategory, User
from app.money import fmt, kopecks_to_stars, parse_rub, smm_cost
from app.services import shop
from app.services.cryptobot import CryptoBotClient
from app.services.errors import InsufficientFunds, OutOfStock, PromoError, ProviderError
from app.services.smm_api import SmmApiError, SmmOrderStatus, SmmPanelClient


def test_money_helpers() -> None:
    assert parse_rub("150") == 15000
    assert parse_rub("150,5") == 15050
    assert parse_rub(" 1 000.25 ") == 100025
    assert parse_rub("0") is None
    assert parse_rub("-5") is None
    assert parse_rub("1.234") is None
    assert parse_rub("abc") is None
    assert parse_rub("nan") is None
    assert fmt(150000) == "1 500 ₽"
    assert fmt(15050) == "150.50 ₽"
    assert smm_cost(15000, 100) == 1500
    assert smm_cost(1, 1) == 1  # always rounds up
    assert kopecks_to_stars(15000, Decimal("1.5")) == 100
    assert kopecks_to_stars(15001, Decimal("1.5")) == 101


async def _user(session, user_id: int, balance: int = 0) -> User:  # type: ignore[no-untyped-def]
    user, _ = await shop.get_or_create_user(session, user_id, f"u{user_id}")
    user.balance = balance
    await session.commit()
    return user


async def _product(session, price: int, stock: list[str]) -> Subcategory:  # type: ignore[no-untyped-def]
    cat = Category(kind=CategoryKind.PRODUCT, name="Accounts")
    session.add(cat)
    await session.flush()
    sub = Subcategory(category_id=cat.id, name="Steam", price=price)
    session.add(sub)
    await session.flush()
    session.add_all(StockItem(subcategory_id=sub.id, content=c) for c in stock)
    await session.commit()
    return sub


async def test_buy_product_charges_and_removes_stock(sessionmaker) -> None:  # type: ignore[no-untyped-def]
    async with sessionmaker() as session:
        await _user(session, 1, balance=1000)
        sub = await _product(session, 300, ["a", "b", "c"])
        order = await shop.buy_product(session, 1, sub.id, 2)
        assert [d["content"] for d in order.delivered] == ["a", "b"]
        assert order.amount == 600
        assert (await shop.refresh_user(session, 1)).balance == 400
        assert await shop.stock_count(session, sub.id) == 1


async def test_buy_product_insufficient_and_out_of_stock(sessionmaker) -> None:  # type: ignore[no-untyped-def]
    async with sessionmaker() as session:
        await _user(session, 1, balance=500)
        sub = await _product(session, 300, ["a", "b"])
        sub_id = sub.id  # services roll back on failure, which expires loaded objects
        with pytest.raises(InsufficientFunds):
            await shop.buy_product(session, 1, sub_id, 2)
        with pytest.raises(OutOfStock):
            await shop.buy_product(session, 1, sub_id, 3)
        assert (await shop.refresh_user(session, 1)).balance == 500
        assert await shop.stock_count(session, sub_id) == 2
        assert (await session.scalars(select(Order))).all() == []


class FakeSmm:
    def __init__(self, fail: bool = False, status: SmmOrderStatus | None = None) -> None:
        self.fail, self.status, self.calls = fail, status, []

    async def add_order(self, service_id: str, link: str, quantity: int) -> str:
        self.calls.append((service_id, link, quantity))
        if self.fail:
            raise SmmApiError("boom")
        return "777"

    async def order_status(self, order_id: str) -> SmmOrderStatus:
        assert self.status is not None
        return self.status


async def _service(session) -> SmmService:  # type: ignore[no-untyped-def]
    cat = Category(kind=CategoryKind.SMM, name="Instagram")
    session.add(cat)
    await session.flush()
    sub = Subcategory(category_id=cat.id, name="Followers")
    session.add(sub)
    await session.flush()
    svc = SmmService(
        subcategory_id=sub.id,
        name="Followers HQ",
        panel_service_id="12",
        price_per_1000=10000,
        min_qty=100,
        max_qty=5000,
    )
    session.add(svc)
    await session.commit()
    return svc


async def test_smm_order_refunds_when_panel_fails(sessionmaker) -> None:  # type: ignore[no-untyped-def]
    async with sessionmaker() as session:
        await _user(session, 1, balance=5000)
        svc = await _service(session)
        with pytest.raises(ProviderError):
            await shop.buy_smm(session, FakeSmm(fail=True), 1, svc.id, 200, "https://instagram.com/x")  # type: ignore[arg-type]
        assert (await shop.refresh_user(session, 1)).balance == 5000
        order = (await session.scalars(select(Order))).one()
        assert order.status == OrderStatus.FAILED and order.refunded == 2000


async def test_smm_partial_refund_happens_once(sessionmaker) -> None:  # type: ignore[no-untyped-def]
    async with sessionmaker() as session:
        await _user(session, 1, balance=5000)
        svc = await _service(session)
        api = FakeSmm()
        order = await shop.buy_smm(session, api, 1, svc.id, 200, "https://instagram.com/x")  # type: ignore[arg-type]
        assert order.status == OrderStatus.PROCESSING and order.panel_order_id == "777"
        assert (await shop.refresh_user(session, 1)).balance == 3000
        api.status = SmmOrderStatus(status="Partial", remains=50, charge=None)
        order = await shop.sync_smm_order(session, api, order)  # type: ignore[arg-type]
        order = await shop.sync_smm_order(session, api, order)  # type: ignore[arg-type]
        assert order.status == OrderStatus.PARTIAL and order.refunded == 500
        assert (await shop.refresh_user(session, 1)).balance == 3500


async def test_payment_credit_is_idempotent_with_bonus_and_referral(sessionmaker) -> None:  # type: ignore[no-untyped-def]
    async with sessionmaker() as session:
        await _user(session, 1)
        user = await _user(session, 2)
        assert await shop.set_referrer(session, user, 1)
        user.deposit_bonus_percent = 5
        await session.commit()
        payment = await shop.create_payment(session, 2, "cryptobot", 10000)
        result = await shop.complete_payment(session, payment.id, referral_percent=10)
        assert result is not None and result.credited == 10500 and result.referral_reward == 1000
        assert await shop.complete_payment(session, payment.id, referral_percent=10) is None
        assert (await shop.refresh_user(session, 2)).balance == 10500
        referrer = await shop.refresh_user(session, 1)
        assert referrer.balance == 1000 and referrer.referral_earned == 1000


async def test_referrer_rules(sessionmaker) -> None:  # type: ignore[no-untyped-def]
    async with sessionmaker() as session:
        user = await _user(session, 2)
        assert not await shop.set_referrer(session, user, 2)  # self
        assert not await shop.set_referrer(session, user, 999)  # unknown


async def test_promo_limits(sessionmaker) -> None:  # type: ignore[no-untyped-def]
    from app.db.models import Promo

    async with sessionmaker() as session:
        for uid in (1, 2, 3):
            await _user(session, uid)
        session.add(Promo(code="SAKURA", amount=500, max_activations=2, activations=0))
        await session.commit()
        assert await shop.activate_promo(session, 1, "SAKURA") == 500
        with pytest.raises(PromoError):
            await shop.activate_promo(session, 1, "SAKURA")
        await shop.activate_promo(session, 2, " SAKURA ")
        with pytest.raises(PromoError):
            await shop.activate_promo(session, 3, "SAKURA")
        with pytest.raises(PromoError):
            await shop.activate_promo(session, 3, "NOPE")
        assert (await shop.refresh_user(session, 3)).balance == 0


async def test_smm_client_protocol() -> None:
    seen: list[dict[str, str]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        form = dict(httpx.QueryParams(request.content.decode()))
        seen.append(form)
        if form["action"] == "add":
            return httpx.Response(200, json={"order": 23501})
        if form["action"] == "status":
            return httpx.Response(200, json={"status": "Partial", "remains": "157", "charge": "0.27"})
        return httpx.Response(200, json={"error": "Incorrect request"})

    client = SmmPanelClient(
        "https://panel.test/api/v2", "KEY", client=httpx.AsyncClient(transport=httpx.MockTransport(handler))
    )
    assert await client.add_order("1", "https://x", 100) == "23501"
    status = await client.order_status("23501")
    assert status.status == "Partial" and status.remains == 157
    with pytest.raises(SmmApiError):
        await client.balance()
    assert seen[0] == {"key": "KEY", "action": "add", "service": "1", "link": "https://x", "quantity": "100"}


async def test_cryptobot_client_protocol() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.headers["Crypto-Pay-API-Token"] == "TOKEN"
        assert request.url.host == "testnet-pay.crypt.bot"
        body = httpx.Request("POST", "/", content=request.content).read()
        import json

        params = json.loads(body)
        if request.url.path.endswith("createInvoice"):
            assert params["currency_type"] == "fiat" and params["fiat"] == "RUB" and params["amount"] == "150.5"
            return httpx.Response(
                200,
                json={
                    "ok": True,
                    "result": {
                        "invoice_id": 9,
                        "status": "active",
                        "bot_invoice_url": "https://t.me/CryptoTestnetBot?start=IV9",
                    },
                },
            )
        assert params["invoice_ids"] == "9,10"
        return httpx.Response(
            200, json={"ok": True, "result": {"items": [{"invoice_id": 9, "status": "paid", "bot_invoice_url": "u"}]}}
        )

    client = CryptoBotClient("TOKEN", testnet=True, client=httpx.AsyncClient(transport=httpx.MockTransport(handler)))
    inv = await client.create_invoice(15050, description="d", payload="1", expires_in=60)
    assert inv.invoice_id == 9 and inv.url.startswith("https://t.me/")
    found = await client.get_invoices([9, 10])
    assert found[9].status == "paid" and 10 not in found
