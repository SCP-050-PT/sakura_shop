from aiogram.fsm.state import State, StatesGroup


class DepositStates(StatesGroup):
    amount = State()


class ProductStates(StatesGroup):
    quantity = State()


class SmmStates(StatesGroup):
    quantity = State()
    link = State()


class PromoStates(StatesGroup):
    code = State()


class SupportStates(StatesGroup):
    text = State()


class AdminStates(StatesGroup):
    find_user = State()
    balance_change = State()
    bonus = State()
    broadcast = State()
    promo = State()
    ticket_answer = State()
    # Catalog editing
    new_category = State()
    new_subcategory = State()
    new_service = State()
    edit_field = State()
    upload_stock = State()
