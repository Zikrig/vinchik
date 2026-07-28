from __future__ import annotations

from aiogram import Bot, F, Router
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message, ReplyKeyboardRemove
from sqlalchemy.ext.asyncio import AsyncSession

from database.models import Gender, LookingFor, User
from handlers.common import after_profile_ready, load_user, show_my_profile
from keyboards.inline import (
    about_kb,
    gender_kb,
    keep_kb,
    looking_kb,
    location_kb,
    photo_keep_kb,
)
from locales import t
from services.geo import reverse_geocode
from services.users import get_or_create_user
from states.profile import ProfileStates

router = Router()

MAX_PHOTO_BYTES = 5 * 1024 * 1024


async def begin_profile_flow(
    message: Message,
    session: AsyncSession,
    state: FSMContext,
    user: User,
    *,
    refill: bool,
) -> None:
    await state.update_data(refill=refill)
    await state.set_state(ProfileStates.age)
    lang = user.language
    kb = keep_kb(lang, "keep:age") if user.profile and user.profile.age else None
    await message.answer(t("ask_age", lang), reply_markup=kb)


@router.message(ProfileStates.waiting_username)
async def got_username_check(message: Message, session: AsyncSession, state: FSMContext) -> None:
    assert message.from_user
    lang = "ru"
    user = await load_user(session, message.from_user.id)
    if user:
        lang = user.language
    if not message.from_user.username:
        await message.answer(t("need_username", lang))
        return
    user = await get_or_create_user(
        session, message.from_user.id, message.from_user.username, language=lang
    )
    await begin_profile_flow(message, session, state, user, refill=False)


@router.message(ProfileStates.age)
async def set_age(message: Message, session: AsyncSession, state: FSMContext) -> None:
    user = await load_user(session, message.from_user.id)  # type: ignore[union-attr]
    assert user and user.profile
    lang = user.language
    try:
        age = int((message.text or "").strip())
        if not 16 <= age <= 99:
            raise ValueError
    except ValueError:
        await message.answer(t("bad_age", lang))
        return
    user.profile.age = age
    await session.commit()
    await state.set_state(ProfileStates.gender)
    await message.answer(t("ask_gender", lang), reply_markup=gender_kb(lang))


@router.callback_query(ProfileStates.age, F.data == "keep:age")
async def keep_age(callback: CallbackQuery, session: AsyncSession, state: FSMContext) -> None:
    user = await load_user(session, callback.from_user.id)
    assert user and callback.message
    await callback.answer()
    await state.set_state(ProfileStates.gender)
    await callback.message.answer(t("ask_gender", user.language), reply_markup=gender_kb(user.language))


@router.callback_query(ProfileStates.gender, F.data.startswith("gender:"))
async def set_gender(callback: CallbackQuery, session: AsyncSession, state: FSMContext) -> None:
    user = await load_user(session, callback.from_user.id)
    assert user and user.profile and callback.data and callback.message
    gender = callback.data.split(":")[1]
    user.profile.gender = Gender(gender)
    await session.commit()
    await callback.answer()
    await state.set_state(ProfileStates.looking_for)
    await callback.message.edit_text(t("ask_looking", user.language), reply_markup=looking_kb(user.language))


@router.callback_query(ProfileStates.looking_for, F.data.startswith("looking:"))
async def set_looking(callback: CallbackQuery, session: AsyncSession, state: FSMContext) -> None:
    user = await load_user(session, callback.from_user.id)
    assert user and user.profile and callback.data and callback.message
    looking = callback.data.split(":")[1]
    user.profile.looking_for = LookingFor(looking)
    await session.commit()
    await callback.answer()
    await state.set_state(ProfileStates.location)
    reply, inline = location_kb(
        user.language, has_current=user.profile.lat is not None
    )
    await callback.message.answer(t("ask_location", user.language), reply_markup=reply)
    if inline:
        await callback.message.answer(".", reply_markup=inline)


@router.message(ProfileStates.location, F.location)
async def set_location(message: Message, session: AsyncSession, state: FSMContext) -> None:
    user = await load_user(session, message.from_user.id)  # type: ignore[union-attr]
    assert user and user.profile and message.location
    user.profile.lat = message.location.latitude
    user.profile.lon = message.location.longitude
    user.profile.city_name = await reverse_geocode(user.profile.lat, user.profile.lon)
    await session.commit()
    await message.answer("OK", reply_markup=ReplyKeyboardRemove())
    await state.set_state(ProfileStates.name)
    kb = keep_kb(user.language, "keep:name") if user.profile.name else None
    await message.answer(t("ask_name", user.language), reply_markup=kb)


@router.callback_query(ProfileStates.location, F.data == "keep:location")
async def keep_location(callback: CallbackQuery, session: AsyncSession, state: FSMContext) -> None:
    user = await load_user(session, callback.from_user.id)
    assert user and callback.message
    await callback.answer()
    await callback.message.answer("OK", reply_markup=ReplyKeyboardRemove())
    await state.set_state(ProfileStates.name)
    kb = keep_kb(user.language, "keep:name") if user.profile and user.profile.name else None
    await callback.message.answer(t("ask_name", user.language), reply_markup=kb)


@router.message(ProfileStates.name)
async def set_name(message: Message, session: AsyncSession, state: FSMContext) -> None:
    user = await load_user(session, message.from_user.id)  # type: ignore[union-attr]
    assert user and user.profile
    name = (message.text or "").strip()
    if not 2 <= len(name) <= 32:
        await message.answer(t("bad_name", user.language))
        return
    user.profile.name = name
    await session.commit()
    await state.set_state(ProfileStates.about)
    await message.answer(
        t("ask_about", user.language),
        reply_markup=about_kb(user.language, has_current=bool(user.profile.description)),
    )


@router.callback_query(ProfileStates.name, F.data == "keep:name")
async def keep_name(callback: CallbackQuery, session: AsyncSession, state: FSMContext) -> None:
    user = await load_user(session, callback.from_user.id)
    assert user and user.profile and callback.message
    await callback.answer()
    await state.set_state(ProfileStates.about)
    await callback.message.answer(
        t("ask_about", user.language),
        reply_markup=about_kb(user.language, has_current=bool(user.profile.description)),
    )


@router.message(ProfileStates.about)
async def set_about(message: Message, session: AsyncSession, state: FSMContext) -> None:
    user = await load_user(session, message.from_user.id)  # type: ignore[union-attr]
    assert user and user.profile
    text = (message.text or "").strip()[:900]
    user.profile.description = text
    await session.commit()
    await _ask_photo(message, state, user)


@router.callback_query(ProfileStates.about, F.data.in_({"about:skip", "keep:about"}))
async def about_skip_or_keep(
    callback: CallbackQuery, session: AsyncSession, state: FSMContext
) -> None:
    user = await load_user(session, callback.from_user.id)
    assert user and user.profile and callback.message and callback.data
    if callback.data == "about:skip":
        user.profile.description = None
        await session.commit()
    await callback.answer()
    await _ask_photo(callback.message, state, user)


async def _ask_photo(message: Message, state: FSMContext, user: User) -> None:
    await state.set_state(ProfileStates.photo)
    kb = photo_keep_kb(user.language) if user.profile and user.profile.photo_file_id else None
    await message.answer(t("ask_photo", user.language), reply_markup=kb)


@router.message(ProfileStates.photo, F.photo)
async def set_photo(message: Message, session: AsyncSession, state: FSMContext, bot: Bot) -> None:
    await _save_photo_and_finish(message, session, state, bot, from_document=False)


@router.message(ProfileStates.photo, F.document)
async def set_photo_doc(message: Message, session: AsyncSession, state: FSMContext, bot: Bot) -> None:
    await _save_photo_and_finish(message, session, state, bot, from_document=True)


@router.callback_query(ProfileStates.photo, F.data == "keep:photo")
async def keep_photo(callback: CallbackQuery, session: AsyncSession, state: FSMContext) -> None:
    user = await load_user(session, callback.from_user.id)
    assert user and user.profile and callback.message
    if not user.profile.photo_file_id:
        await callback.answer(t("bad_photo", user.language), show_alert=True)
        return
    user.profile.is_complete = True
    user.profile.is_active = True
    await session.commit()
    await callback.answer()
    await state.clear()
    user = await load_user(session, user.tg_id)
    assert user
    await after_profile_ready(callback.message, session, user)


async def _save_photo_and_finish(
    message: Message,
    session: AsyncSession,
    state: FSMContext,
    bot: Bot,
    *,
    from_document: bool,
) -> None:
    user = await load_user(session, message.from_user.id)  # type: ignore[union-attr]
    assert user and user.profile
    lang = user.language
    file_id = None
    if from_document and message.document:
        mime = message.document.mime_type or ""
        name = (message.document.file_name or "").lower()
        ok_mime = mime in {"image/jpeg", "image/png", "image/gif"}
        ok_ext = name.endswith((".jpg", ".jpeg", ".png", ".gif"))
        if not (ok_mime or ok_ext):
            await message.answer(t("bad_photo", lang))
            return
        if message.document.file_size and message.document.file_size > MAX_PHOTO_BYTES:
            await message.answer(t("bad_photo", lang))
            return
        file_id = message.document.file_id
    elif message.photo:
        photo = message.photo[-1]
        try:
            f = await bot.get_file(photo.file_id)
            if f.file_size and f.file_size > MAX_PHOTO_BYTES:
                await message.answer(t("bad_photo", lang))
                return
        except Exception:
            pass
        file_id = photo.file_id
    else:
        await message.answer(t("bad_photo", lang))
        return

    user.profile.photo_file_id = file_id
    user.profile.is_complete = True
    user.profile.is_active = True
    if message.from_user and message.from_user.username:
        user.username = message.from_user.username
    await session.commit()
    await state.clear()
    user = await load_user(session, user.tg_id)
    assert user
    await after_profile_ready(message, session, user)


@router.message(ProfileStates.edit_photo, F.photo)
async def edit_photo(message: Message, session: AsyncSession, state: FSMContext, bot: Bot) -> None:
    user = await load_user(session, message.from_user.id)  # type: ignore[union-attr]
    assert user and user.profile and message.photo
    photo = message.photo[-1]
    try:
        f = await bot.get_file(photo.file_id)
        if f.file_size and f.file_size > MAX_PHOTO_BYTES:
            await message.answer(t("bad_photo", user.language))
            return
    except Exception:
        pass
    user.profile.photo_file_id = photo.file_id
    await session.commit()
    await state.clear()
    await show_my_profile(message, user, user.profile)


@router.message(ProfileStates.edit_text)
async def edit_text(message: Message, session: AsyncSession, state: FSMContext) -> None:
    user = await load_user(session, message.from_user.id)  # type: ignore[union-attr]
    assert user and user.profile
    user.profile.description = (message.text or "").strip()[:900]
    await session.commit()
    await state.clear()
    await show_my_profile(message, user, user.profile)
