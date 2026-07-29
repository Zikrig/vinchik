from aiogram.fsm.state import State, StatesGroup


class BrowseStates(StatesGroup):
    viewing = State()


class MessageStates(StatesGroup):
    content = State()
    attachments = State()
