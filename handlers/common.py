from __future__ import annotations

from aiogram.types import Message
from aiogram.types import ReplyKeyboardRemove
from sqlalchemy.ext.asyncio import AsyncSession

from database.models import Profile, User
from keyboards.inline import main_menu_kb, my_profile_kb
from locales import t
from services.browse import profile_caption
from services.media import as_photo_input
from services.settings_service import is_registration_only


async def load_user(session: AsyncSession, tg_id: int) -> User | None:
    from services.users import load_user_with_profile

    return await load_user_with_profile(session, tg_id)


async def show_my_profile(message: Message, user: User, profile: Profile) -> None:
    lang = user.language
    caption = profile_caption(profile)
    kb = my_profile_kb(lang)
    photo = as_photo_input(profile.photo_file_id)
    if photo is not None:
        await message.answer_photo(photo, caption=caption, reply_markup=kb)
    else:
        await message.answer(caption, reply_markup=kb)


async def show_main_menu(message: Message, user: User) -> None:
    await message.answer("☰", reply_markup=main_menu_kb(user.language))


async def after_profile_ready(message: Message, session: AsyncSession, user: User) -> None:
    from handlers.browse import start_browse

    if await is_registration_only(session):
        await message.answer(
            t("soft_launch", user.language),
            reply_markup=ReplyKeyboardRemove(),
        )
        await show_my_profile(message, user, user.profile)  # type: ignore[arg-type]
        return
    await message.answer(".", reply_markup=ReplyKeyboardRemove())
    await start_browse(message, session, user)
