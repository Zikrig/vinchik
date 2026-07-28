from aiogram import F, Router
from aiogram.filters import StateFilter
from aiogram.types import Message
from sqlalchemy.ext.asyncio import AsyncSession

from handlers.common import show_main_menu
from keyboards.inline import language_kb
from locales import t
from services.users import get_or_create_user

router = Router()


@router.message(StateFilter(None), F.chat.type == "private")
async def out_of_scenario_message(message: Message, session: AsyncSession) -> None:
    """Any message outside FSM → main menu."""
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
        await message.answer(
            t("choose_language", user.language or "ru"),
            reply_markup=language_kb(),
        )
        return
    await show_main_menu(message, user)
