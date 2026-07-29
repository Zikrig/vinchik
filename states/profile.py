from aiogram.fsm.state import State, StatesGroup


class ProfileStates(StatesGroup):
    waiting_username = State()
    age = State()
    gender = State()
    looking_for = State()
    location = State()
    location_text = State()
    location_confirm = State()
    name = State()
    about = State()
    photo = State()
    edit_photo = State()
    edit_text = State()
    send_message = State()
