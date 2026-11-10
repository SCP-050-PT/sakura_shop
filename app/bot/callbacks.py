from aiogram.filters.callback_data import CallbackData


class Nav(CallbackData, prefix="nav"):
    to: str


class CatalogCb(CallbackData, prefix="cat"):
    kind: str


class CategoryCb(CallbackData, prefix="c"):
    id: int


class SubcategoryCb(CallbackData, prefix="s"):
    id: int


class BuyProductCb(CallbackData, prefix="bp"):
    sub_id: int


class ServiceCb(CallbackData, prefix="svc"):
    id: int


class BuyServiceCb(CallbackData, prefix="bs"):
    id: int


class OrderCb(CallbackData, prefix="o"):
    id: int
    action: str = "view"


class PayMethodCb(CallbackData, prefix="pm"):
    method: str


class CheckPaymentCb(CallbackData, prefix="pc"):
    id: int


class SupportTopicCb(CallbackData, prefix="st"):
    topic: str


# ---------------------------------------------------------------- admin


class AdminCb(CallbackData, prefix="a"):
    action: str


class AdminUserCb(CallbackData, prefix="au"):
    action: str
    user_id: int


class AdminCatalogCb(CallbackData, prefix="ac"):
    action: str
    id: int = 0
    arg: str = ""


class AdminTicketCb(CallbackData, prefix="at"):
    action: str
    id: int
