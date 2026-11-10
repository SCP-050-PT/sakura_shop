class ShopError(Exception):
    """Business error whose message is safe to show to the user."""


class InsufficientFunds(ShopError):
    def __init__(self) -> None:
        super().__init__("❗️ Недостаточно средств на балансе.")


class OutOfStock(ShopError):
    def __init__(self, available: int) -> None:
        self.available = available
        super().__init__(f"❗️ В наличии только {available} шт.")


class InvalidQuantity(ShopError):
    pass


class OrderNeedsReview(ShopError):
    def __init__(self, order_id: int) -> None:
        self.order_id = order_id
        super().__init__(
            f"⏳ Сервис накрутки не ответил. Заказ #{order_id} передан администратору на проверку: "
            "он будет выполнен, либо деньги вернутся на баланс."
        )


class NotFound(ShopError):
    def __init__(self) -> None:
        super().__init__("❗️ Не найдено. Возможно, позиция была удалена.")


class PromoError(ShopError):
    pass


class ProviderError(ShopError):
    pass
