from aiogram.fsm.state import State, StatesGroup


class AdminStates(StatesGroup):
    edit_limit = State()
    edit_dist = State()
    edit_reshow = State()
    edit_card = State()
    edit_check_time = State()
    edit_manager = State()
    edit_support = State()
    edit_welcome = State()
    add_channel = State()
    link_create_name = State()
    link_rename = State()
