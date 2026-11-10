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


class NotFound(ShopError):
    def __init__(self) -> None:
        super().__init__("❗️ Не найдено. Возможно, позиция была удалена.")


class PromoError(ShopError):
    pass


class ProviderError(ShopError):
    pass
