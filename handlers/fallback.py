from aiogram import F, Router
from aiogram.filters import StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.types import Message
from sqlalchemy.ext.asyncio import AsyncSession

from handlers.common import show_main_menu
from handlers.profile import begin_profile_flow
from keyboards.inline import language_kb
from locales import t
from services.users import get_or_create_user
from states.profile import ProfileStates

router = Router()


@router.message(StateFilter(None), F.chat.type == "private")
async def out_of_scenario_message(
    message: Message, session: AsyncSession, state: FSMContext
) -> None:
    """Any message outside FSM → main menu or continue registration."""
    if not message.from_user:
        return
    user = await get_or_create_user(
        session,
        message.from_user.id,
        message.from_user.username,
    )
    if user.is_blocked:
        await message.answer(t("you_are_blocked", user.language))
        return
    if not (user.profile and user.profile.is_complete):
        lang = user.language or "ru"
        if not user.language_chosen:
            await message.answer(t("choose_language", lang), reply_markup=language_kb())
            return
        if not message.from_user.username:
            await state.set_state(ProfileStates.waiting_username)
            await message.answer(t("need_username", lang))
            return
        await begin_profile_flow(message, session, state, user, refill=False)
        return
    await show_main_menu(message, user)
