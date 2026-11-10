from datetime import UTC, datetime
from enum import StrEnum
from typing import Any

from sqlalchemy import (
    JSON,
    BigInteger,
    Boolean,
    DateTime,
    ForeignKey,
    Integer,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


def utcnow() -> datetime:
    """Naive UTC, matching SQLite CURRENT_TIMESTAMP, so stored and compared times share one clock."""
    return datetime.now(UTC).replace(tzinfo=None)


class Base(DeclarativeBase):
    pass


class CategoryKind(StrEnum):
    PRODUCT = "product"
    SMM = "smm"


class StockKind(StrEnum):
    TEXT = "text"
    FILE = "file"


class OrderStatus(StrEnum):
    PENDING = "pending"
    PROCESSING = "processing"
    COMPLETED = "completed"
    PARTIAL = "partial"
    FAILED = "failed"
    # Panel outcome unknown (timeout or crash mid-order); money is held until an admin resolves it.
    REVIEW = "review"


class PaymentStatus(StrEnum):
    PENDING = "pending"
    PAID = "paid"
    EXPIRED = "expired"
    REFUNDED = "refunded"


class TicketStatus(StrEnum):
    OPEN = "open"
    CLOSED = "closed"


class User(Base):
    __tablename__ = "users"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=False)
    username: Mapped[str | None] = mapped_column(String(64))
    balance: Mapped[int] = mapped_column(BigInteger, default=0)
    total_deposited: Mapped[int] = mapped_column(BigInteger, default=0)
    referrer_id: Mapped[int | None] = mapped_column(BigInteger, ForeignKey("users.id", ondelete="SET NULL"), index=True)
    referral_earned: Mapped[int] = mapped_column(BigInteger, default=0)
    deposit_bonus_percent: Mapped[int] = mapped_column(Integer, default=0)
    is_banned: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, server_default=func.now())


class Category(Base):
    __tablename__ = "categories"

    id: Mapped[int] = mapped_column(primary_key=True)
    kind: Mapped[str] = mapped_column(String(16), index=True)
    name: Mapped[str] = mapped_column(String(128))

    subcategories: Mapped[list["Subcategory"]] = relationship(
        back_populates="category", cascade="all, delete-orphan", order_by="Subcategory.id"
    )


class Subcategory(Base):
    __tablename__ = "subcategories"

    id: Mapped[int] = mapped_column(primary_key=True)
    category_id: Mapped[int] = mapped_column(ForeignKey("categories.id", ondelete="CASCADE"), index=True)
    name: Mapped[str] = mapped_column(String(128))
    description: Mapped[str] = mapped_column(Text, default="")
    # Unit price; used only by product subcategories.
    price: Mapped[int] = mapped_column(BigInteger, default=0)

    category: Mapped[Category] = relationship(back_populates="subcategories")
    stock: Mapped[list["StockItem"]] = relationship(cascade="all, delete-orphan", passive_deletes=True)
    services: Mapped[list["SmmService"]] = relationship(
        back_populates="subcategory", cascade="all, delete-orphan", order_by="SmmService.id"
    )


class StockItem(Base):
    __tablename__ = "stock_items"

    id: Mapped[int] = mapped_column(primary_key=True)
    subcategory_id: Mapped[int] = mapped_column(ForeignKey("subcategories.id", ondelete="CASCADE"), index=True)
    kind: Mapped[str] = mapped_column(String(8), default=StockKind.TEXT)
    # Text content or Telegram file_id.
    content: Mapped[str] = mapped_column(Text)
    file_name: Mapped[str | None] = mapped_column(String(255))


class SmmService(Base):
    __tablename__ = "smm_services"

    id: Mapped[int] = mapped_column(primary_key=True)
    subcategory_id: Mapped[int] = mapped_column(ForeignKey("subcategories.id", ondelete="CASCADE"), index=True)
    name: Mapped[str] = mapped_column(String(128))
    description: Mapped[str] = mapped_column(Text, default="")
    panel_service_id: Mapped[str] = mapped_column(String(32))
    price_per_1000: Mapped[int] = mapped_column(BigInteger)
    min_qty: Mapped[int] = mapped_column(Integer, default=10)
    max_qty: Mapped[int] = mapped_column(Integer, default=10000)

    subcategory: Mapped[Subcategory] = relationship(back_populates="services")


class Order(Base):
    __tablename__ = "orders"

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("users.id"), index=True)
    kind: Mapped[str] = mapped_column(String(16))
    title: Mapped[str] = mapped_column(String(256))
    quantity: Mapped[int] = mapped_column(Integer)
    amount: Mapped[int] = mapped_column(BigInteger)
    refunded: Mapped[int] = mapped_column(BigInteger, default=0)
    status: Mapped[str] = mapped_column(String(16), default=OrderStatus.PENDING, index=True)
    link: Mapped[str | None] = mapped_column(String(512))
    panel_order_id: Mapped[str | None] = mapped_column(String(64))
    panel_status: Mapped[str | None] = mapped_column(String(32))
    # Copy of delivered stock so purchases stay viewable after stock is removed.
    delivered: Mapped[list[dict[str, Any]] | None] = mapped_column(JSON)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, server_default=func.now())


class Payment(Base):
    __tablename__ = "payments"
    __table_args__ = (UniqueConstraint("provider", "external_id"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("users.id"), index=True)
    provider: Mapped[str] = mapped_column(String(16))
    amount: Mapped[int] = mapped_column(BigInteger)
    status: Mapped[str] = mapped_column(String(16), default=PaymentStatus.PENDING, index=True)
    external_id: Mapped[str | None] = mapped_column(String(128))
    pay_url: Mapped[str | None] = mapped_column(String(512))
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, server_default=func.now())
    paid_at: Mapped[datetime | None] = mapped_column(DateTime)


class Promo(Base):
    __tablename__ = "promos"

    id: Mapped[int] = mapped_column(primary_key=True)
    code: Mapped[str] = mapped_column(String(64), unique=True)
    amount: Mapped[int] = mapped_column(BigInteger)
    max_activations: Mapped[int] = mapped_column(Integer, default=1)
    activations: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, server_default=func.now())


class PromoActivation(Base):
    __tablename__ = "promo_activations"
    __table_args__ = (UniqueConstraint("promo_id", "user_id"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    promo_id: Mapped[int] = mapped_column(ForeignKey("promos.id", ondelete="CASCADE"))
    user_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("users.id"))


class SupportTicket(Base):
    __tablename__ = "support_tickets"

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("users.id"), index=True)
    topic: Mapped[str] = mapped_column(String(32))
    text: Mapped[str] = mapped_column(Text)
    status: Mapped[str] = mapped_column(String(16), default=TicketStatus.OPEN, index=True)
    answer: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, server_default=func.now())
