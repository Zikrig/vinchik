from aiogram import F, Router
from aiogram.filters import CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message
from sqlalchemy.ext.asyncio import AsyncSession

from handlers.common import load_user, show_main_menu, show_my_profile
from keyboards.inline import language_kb
from locales import t
from services.users import get_or_create_user, set_language
from states.profile import ProfileStates

router = Router()


@router.message(CommandStart())
async def cmd_start(message: Message, session: AsyncSession, state: FSMContext) -> None:
    await state.clear()
    assert message.from_user
    user = await get_or_create_user(
        session,
        message.from_user.id,
        message.from_user.username,
    )
    if user.is_blocked:
        await message.answer(t("you_are_blocked", user.language))
        return
    profile = user.profile
    if profile and profile.is_complete:
        await show_main_menu(message, user)
        return
    # first-time or incomplete: language if never chosen explicitly — always ask if no complete profile
    await message.answer(t("choose_language", "ru"), reply_markup=language_kb())


@router.callback_query(F.data.startswith("lang:"))
async def on_language(callback: CallbackQuery, session: AsyncSession, state: FSMContext) -> None:
    assert callback.from_user and callback.message and callback.data
    lang = callback.data.split(":", 1)[1]
    if lang not in {"ru", "tg"}:
        await callback.answer()
        return
    user = await get_or_create_user(
        session, callback.from_user.id, callback.from_user.username, language=lang
    )
    await set_language(session, user, lang)
    await callback.answer()
    await callback.message.edit_text(t("language_saved", lang))

    user = await load_user(session, callback.from_user.id)
    assert user
    if user.profile and user.profile.is_complete:
        await show_main_menu(callback.message, user)
        return

    if not callback.from_user.username:
        await state.set_state(ProfileStates.waiting_username)
        await callback.message.answer(t("need_username", lang))
        return

    from handlers.profile import begin_profile_flow

    await begin_profile_flow(callback.message, session, state, user, refill=False)
