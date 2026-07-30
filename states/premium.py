from aiogram.fsm.state import State, StatesGroup


class PremiumStates(StatesGroup):
    awaiting_receipt = State()
