from __future__ import annotations

from aiogram.fsm.context import FSMContext
from aiogram.types import Message
from aiogram.types import ReplyKeyboardRemove
from sqlalchemy.ext.asyncio import AsyncSession

from database.models import Profile, User
from keyboards.inline import main_menu_kb, my_profile_kb, premium_cta_kb
from locales import t
from services.browse import profile_caption
from services.media import as_photo_input
from services.settings_service import is_registration_only
from services.users import is_premium


async def load_user(session: AsyncSession, tg_id: int) -> User | None:
    from services.users import load_user_with_profile

    return await load_user_with_profile(session, tg_id)


async def show_my_profile(message: Message, user: User, profile: Profile) -> None:
    from services.media import media_photos_for_profile, profile_photo_ids

    lang = user.language
    caption = profile_caption(profile)
    kb = my_profile_kb(lang)
    photos = profile_photo_ids(profile)
    if not photos:
        await message.answer(caption, reply_markup=kb)
        return
    if len(photos) == 1:
        photo = as_photo_input(photos[0])
        if photo is not None:
            await message.answer_photo(photo, caption=caption, reply_markup=kb)
        else:
            await message.answer(caption, reply_markup=kb)
        return
    media = media_photos_for_profile(profile, caption=caption)
    if not media:
        await message.answer(caption, reply_markup=kb)
        return
    await message.answer_media_group(media)
    await message.answer("☰", reply_markup=kb)


async def show_main_menu(message: Message, user: User) -> None:
    await message.answer("☰", reply_markup=main_menu_kb(user.language))


async def after_profile_ready(
    message: Message,
    session: AsyncSession,
    user: User,
    state: FSMContext | None = None,
) -> None:
    from handlers.browse import start_browse

    lang = user.language or "ru"
    if await is_registration_only(session):
        await message.answer(
            t("soft_launch", lang),
            reply_markup=ReplyKeyboardRemove(),
        )
        if not is_premium(user):
            await message.answer(
                t("premium_benefits", lang),
                reply_markup=premium_cta_kb(lang),
            )
        await show_my_profile(message, user, user.profile)  # type: ignore[arg-type]
        return
    await message.answer(".", reply_markup=ReplyKeyboardRemove())
    if not is_premium(user):
        await message.answer(
            t("premium_promo", lang),
            reply_markup=premium_cta_kb(lang),
        )
    await start_browse(message, session, user, state=state)
