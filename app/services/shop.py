from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm.exc import StaleDataError

from app.db.models import (
    Category,
    CategoryKind,
    Order,
    OrderStatus,
    Payment,
    PaymentStatus,
    Promo,
    PromoActivation,
    SmmService,
    StockItem,
    Subcategory,
    SupportTicket,
    TicketStatus,
    User,
)
from app.money import smm_cost
from app.services.errors import (
    InsufficientFunds,
    InvalidQuantity,
    NotFound,
    OutOfStock,
    PromoError,
    ProviderError,
    ShopError,
)
from app.services.smm_api import SmmApiError, SmmPanelClient

# ---------------------------------------------------------------- users


async def get_or_create_user(session: AsyncSession, user_id: int, username: str | None) -> tuple[User, bool]:
    user = await session.get(User, user_id)
    if user is not None:
        if user.username != username:
            user.username = username
        return user, False
    user = User(id=user_id, username=username, balance=0, total_deposited=0, referral_earned=0)
    session.add(user)
    await session.flush()
    return user, True


async def set_referrer(session: AsyncSession, user: User, referrer_id: int) -> bool:
    if user.referrer_id is not None or referrer_id == user.id:
        return False
    if await session.get(User, referrer_id) is None:
        return False
    user.referrer_id = referrer_id
    await session.flush()
    return True


async def count_referrals(session: AsyncSession, user_id: int) -> int:
    return await session.scalar(select(func.count()).where(User.referrer_id == user_id)) or 0


async def change_balance(session: AsyncSession, user_id: int, delta: int) -> bool:
    """Atomically add delta; refuses to go below zero."""
    stmt = update(User).where(User.id == user_id).values(balance=User.balance + delta)
    if delta < 0:
        stmt = stmt.where(User.balance >= -delta)
    result = await session.execute(stmt.execution_options(synchronize_session=False))
    return result.rowcount == 1


async def refresh_user(session: AsyncSession, user_id: int) -> User:
    user = await session.get(User, user_id, populate_existing=True)
    if user is None:
        raise NotFound()
    return user


# ---------------------------------------------------------------- catalog


async def list_categories(session: AsyncSession, kind: CategoryKind) -> list[Category]:
    return list(await session.scalars(select(Category).where(Category.kind == kind).order_by(Category.id)))


async def list_subcategories(session: AsyncSession, category_id: int) -> list[Subcategory]:
    return list(
        await session.scalars(
            select(Subcategory).where(Subcategory.category_id == category_id).order_by(Subcategory.id)
        )
    )


async def list_services(session: AsyncSession, subcategory_id: int) -> list[SmmService]:
    return list(
        await session.scalars(
            select(SmmService).where(SmmService.subcategory_id == subcategory_id).order_by(SmmService.id)
        )
    )


async def stock_count(session: AsyncSession, subcategory_id: int) -> int:
    return await session.scalar(select(func.count()).where(StockItem.subcategory_id == subcategory_id)) or 0


# ---------------------------------------------------------------- purchases


async def buy_product(session: AsyncSession, user_id: int, subcategory_id: int, quantity: int) -> Order:
    sub = await session.get(Subcategory, subcategory_id)
    if sub is None:
        raise NotFound()
    if quantity < 1:
        raise InvalidQuantity("❗️ Количество должно быть больше нуля.")

    items = list(
        await session.scalars(
            select(StockItem)
            .where(StockItem.subcategory_id == subcategory_id)
            .order_by(StockItem.id)
            .limit(quantity)
            .with_for_update(skip_locked=True)
        )
    )
    if len(items) < quantity:
        raise OutOfStock(len(items))

    amount = sub.price * quantity
    if not await change_balance(session, user_id, -amount):
        await session.rollback()
        raise InsufficientFunds()

    order = Order(
        user_id=user_id,
        kind=CategoryKind.PRODUCT,
        title=sub.name,
        quantity=quantity,
        amount=amount,
        refunded=0,
        status=OrderStatus.COMPLETED,
        delivered=[{"kind": i.kind, "content": i.content, "file_name": i.file_name} for i in items],
    )
    session.add(order)
    for item in items:
        await session.delete(item)
    try:
        await session.commit()
    except StaleDataError:
        # A concurrent purchase took the same items first.
        await session.rollback()
        raise ShopError("❗️ Товар только что раскупили, попробуйте ещё раз.") from None
    return order


def validate_link(link: str) -> str:
    link = link.strip()
    if not link or len(link) > 512 or any(ch.isspace() for ch in link):
        raise InvalidQuantity("❗️ Некорректная ссылка.")
    return link


async def buy_smm(
    session: AsyncSession, api: SmmPanelClient | None, user_id: int, service_id: int, quantity: int, link: str
) -> Order:
    if api is None:
        raise ProviderError("❗️ Накрутка временно недоступна.")
    service = await session.get(SmmService, service_id)
    if service is None:
        raise NotFound()
    if not service.min_qty <= quantity <= service.max_qty:
        raise InvalidQuantity(f"❗️ Количество должно быть от {service.min_qty} до {service.max_qty}.")
    link = validate_link(link)

    amount = smm_cost(service.price_per_1000, quantity)
    if not await change_balance(session, user_id, -amount):
        await session.rollback()
        raise InsufficientFunds()
    order = Order(
        user_id=user_id,
        kind=CategoryKind.SMM,
        title=service.name,
        quantity=quantity,
        amount=amount,
        refunded=0,
        status=OrderStatus.PENDING,
        link=link,
    )
    session.add(order)
    # Commit the charge before calling the panel so a crash can never lose money silently.
    await session.commit()

    try:
        order.panel_order_id = await api.add_order(service.panel_service_id, link, quantity)
    except SmmApiError:
        await change_balance(session, user_id, amount)
        order.status = OrderStatus.FAILED
        order.refunded = amount
        await session.commit()
        raise ProviderError("❗️ Сервис накрутки отклонил заказ. Деньги возвращены на баланс.") from None
    order.status = OrderStatus.PROCESSING
    await session.commit()
    return order


SMM_FINAL_OK = {"completed"}
SMM_FINAL_CANCELED = {"canceled", "cancelled", "refunded", "fail", "failed"}
SMM_FINAL_PARTIAL = {"partial"}


async def sync_smm_order(session: AsyncSession, api: SmmPanelClient, order: Order) -> Order:
    """Pull status from the panel and refund canceled/partial orders exactly once."""
    if order.status != OrderStatus.PROCESSING or not order.panel_order_id:
        return order
    status = await api.order_status(order.panel_order_id)
    order.panel_status = status.status
    normalized = status.status.strip().lower()

    if normalized in SMM_FINAL_OK:
        order.status = OrderStatus.COMPLETED
    elif normalized in SMM_FINAL_CANCELED | SMM_FINAL_PARTIAL:
        if normalized in SMM_FINAL_PARTIAL and status.remains is not None:
            remains = max(0, min(status.remains, order.quantity))
            refund = order.amount * remains // order.quantity
            new_status = OrderStatus.PARTIAL
        else:
            refund = order.amount
            new_status = OrderStatus.FAILED
        claimed = await session.execute(
            update(Order)
            .where(Order.id == order.id, Order.status == OrderStatus.PROCESSING)
            .values(status=new_status, refunded=refund, panel_status=status.status)
            .execution_options(synchronize_session=False)
        )
        if claimed.rowcount == 1 and refund:
            await change_balance(session, order.user_id, refund)
        await session.commit()
        await session.refresh(order)
        return order
    await session.commit()
    return order


async def user_orders(session: AsyncSession, user_id: int, limit: int = 10) -> list[Order]:
    return list(
        await session.scalars(select(Order).where(Order.user_id == user_id).order_by(Order.id.desc()).limit(limit))
    )


# ---------------------------------------------------------------- payments


@dataclass(slots=True)
class CreditResult:
    payment: Payment
    credited: int
    bonus: int
    referrer_id: int | None
    referral_reward: int


async def create_payment(session: AsyncSession, user_id: int, provider: str, amount: int) -> Payment:
    payment = Payment(user_id=user_id, provider=provider, amount=amount, status=PaymentStatus.PENDING)
    session.add(payment)
    await session.commit()
    return payment


async def complete_payment(
    session: AsyncSession, payment_id: int, referral_percent: int, external_id: str | None = None
) -> CreditResult | None:
    """Credit a pending payment. Returns None if it was already processed (idempotent)."""
    values: dict[str, object] = {"status": PaymentStatus.PAID, "paid_at": datetime.now()}
    if external_id is not None:
        values["external_id"] = external_id
    claimed = await session.execute(
        update(Payment)
        .where(Payment.id == payment_id, Payment.status == PaymentStatus.PENDING)
        .values(**values)
        .execution_options(synchronize_session=False)
    )
    if claimed.rowcount != 1:
        await session.rollback()
        return None

    payment = await session.get(Payment, payment_id, populate_existing=True)
    assert payment is not None
    user = await refresh_user(session, payment.user_id)
    bonus = payment.amount * user.deposit_bonus_percent // 100
    credited = payment.amount + bonus
    await session.execute(
        update(User)
        .where(User.id == user.id)
        .values(balance=User.balance + credited, total_deposited=User.total_deposited + payment.amount)
        .execution_options(synchronize_session=False)
    )
    reward = 0
    if user.referrer_id and referral_percent > 0:
        reward = payment.amount * referral_percent // 100
        if reward:
            await session.execute(
                update(User)
                .where(User.id == user.referrer_id)
                .values(balance=User.balance + reward, referral_earned=User.referral_earned + reward)
                .execution_options(synchronize_session=False)
            )
    await session.commit()
    return CreditResult(payment, credited, bonus, user.referrer_id, reward)


async def expire_payment(session: AsyncSession, payment_id: int) -> None:
    await session.execute(
        update(Payment)
        .where(Payment.id == payment_id, Payment.status == PaymentStatus.PENDING)
        .values(status=PaymentStatus.EXPIRED)
        .execution_options(synchronize_session=False)
    )
    await session.commit()


# ---------------------------------------------------------------- promo


async def activate_promo(session: AsyncSession, user_id: int, code: str) -> int:
    promo = await session.scalar(select(Promo).where(Promo.code == code.strip()))
    if promo is None:
        raise PromoError("❗️ Такого промокода не существует.")
    already = await session.scalar(
        select(PromoActivation.id).where(PromoActivation.promo_id == promo.id, PromoActivation.user_id == user_id)
    )
    if already:
        raise PromoError("❗️ Вы уже активировали этот промокод.")
    claimed = await session.execute(
        update(Promo)
        .where(Promo.id == promo.id, Promo.activations < Promo.max_activations)
        .values(activations=Promo.activations + 1)
        .execution_options(synchronize_session=False)
    )
    if claimed.rowcount != 1:
        await session.rollback()
        raise PromoError("❗️ Промокод больше не действует.")
    session.add(PromoActivation(promo_id=promo.id, user_id=user_id))
    await change_balance(session, user_id, promo.amount)
    await session.commit()
    return promo.amount


# ---------------------------------------------------------------- support


async def open_ticket(session: AsyncSession, user_id: int, topic: str, text: str) -> SupportTicket:
    existing = await session.scalar(
        select(SupportTicket.id).where(SupportTicket.user_id == user_id, SupportTicket.status == TicketStatus.OPEN)
    )
    if existing:
        raise ShopError("❗️ У вас уже есть открытое обращение. Дождитесь ответа.")
    ticket = SupportTicket(user_id=user_id, topic=topic, text=text, status=TicketStatus.OPEN)
    session.add(ticket)
    await session.commit()
    return ticket


async def has_open_ticket(session: AsyncSession, user_id: int) -> bool:
    return bool(
        await session.scalar(
            select(SupportTicket.id).where(SupportTicket.user_id == user_id, SupportTicket.status == TicketStatus.OPEN)
        )
    )


# ---------------------------------------------------------------- stats


@dataclass(slots=True)
class Stats:
    users: int
    users_with_balance: int
    balances_total: int
    deposited_total: int
    product_orders: int
    product_revenue: int
    smm_orders: int
    smm_revenue: int


async def collect_stats(session: AsyncSession) -> Stats:
    users, with_balance, balances, deposited = (
        await session.execute(
            select(
                func.count(User.id),
                func.count(User.id).filter(User.balance > 0),
                func.coalesce(func.sum(User.balance), 0),
                func.coalesce(func.sum(User.total_deposited), 0),
            )
        )
    ).one()

    def orders_of(kind: CategoryKind):  # type: ignore[no-untyped-def]
        return select(func.count(Order.id), func.coalesce(func.sum(Order.amount - Order.refunded), 0)).where(
            Order.kind == kind, Order.status != OrderStatus.FAILED
        )

    p_count, p_sum = (await session.execute(orders_of(CategoryKind.PRODUCT))).one()
    s_count, s_sum = (await session.execute(orders_of(CategoryKind.SMM))).one()
    return Stats(users, with_balance, balances, deposited, p_count, p_sum, s_count, s_sum)
