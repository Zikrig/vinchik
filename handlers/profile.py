from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from aiogram import Bot, F, Router
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message, ReplyKeyboardRemove
from sqlalchemy.ext.asyncio import AsyncSession

from database.models import Gender, LookingFor, Profile, User
from handlers.common import (
    after_profile_ready,
    callback_context,
    drop_reply_keyboard,
    ensure_user,
    load_user,
    message_user,
    show_my_profile,
)
from keyboards.inline import (
    about_kb,
    gender_kb,
    keep_kb,
    looking_kb,
    location_confirm_kb,
    location_kb,
    location_pick_kb,
    photo_step_kb,
)
from locales import t
from services.geo import reverse_geocode
from services.media import MAX_PROFILE_PHOTOS, profile_photo_ids, set_profile_photos
from services.settlements import (
    choice_button_label,
    disambiguation_choices,
    format_confirm,
    get_settlement,
    nearest_settlements,
    search_settlements,
)
from services.users import get_or_create_user
from states.profile import ProfileStates

router = Router()

MAX_PHOTO_BYTES = 5 * 1024 * 1024

# Telegram delivers an album as separate updates and aiogram runs them
# concurrently, so read-modify-write of draft_photos must be serialized.
_photo_locks: dict[int, asyncio.Lock] = {}
_photo_lock_holders: dict[int, int] = {}


@asynccontextmanager
async def _photo_draft_lock(user_id: int) -> AsyncIterator[None]:
    lock = _photo_locks.get(user_id)
    if lock is None:
        lock = asyncio.Lock()
        _photo_locks[user_id] = lock
    _photo_lock_holders[user_id] = _photo_lock_holders.get(user_id, 0) + 1
    try:
        async with lock:
            yield
    finally:
        rest = _photo_lock_holders.get(user_id, 1) - 1
        if rest <= 0:
            _photo_lock_holders.pop(user_id, None)
            _photo_locks.pop(user_id, None)
        else:
            _photo_lock_holders[user_id] = rest


async def _profile_owner(
    message: Message, session: AsyncSession, state: FSMContext
) -> tuple[User, Profile] | None:
    """User whose profile is being filled in; drops the state if the row is gone."""
    user = await message_user(message, session)
    if user is None or user.profile is None:
        await state.clear()
        return None
    return user, user.profile


async def _profile_owner_cb(
    callback: CallbackQuery, session: AsyncSession, state: FSMContext
) -> tuple[User, Profile, Message] | None:
    ctx = await callback_context(callback, session)
    if ctx is None:
        return None
    user, message = ctx
    if user.profile is None:
        await state.clear()
        return None
    return user, user.profile, message


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
    if message.from_user is None:
        return
    user = await ensure_user(session, message.from_user.id, message.from_user.username)
    lang = user.language or "ru"
    if not message.from_user.username:
        await message.answer(t("need_username", lang))
        return
    user = await get_or_create_user(
        session, message.from_user.id, message.from_user.username, language=lang
    )
    await begin_profile_flow(message, session, state, user, refill=False)


@router.message(ProfileStates.age)
async def set_age(message: Message, session: AsyncSession, state: FSMContext) -> None:
    owner = await _profile_owner(message, session, state)
    if owner is None:
        return
    user, profile = owner
    lang = user.language
    try:
        age = int((message.text or "").strip())
        if not 16 <= age <= 99:
            raise ValueError
    except ValueError:
        await message.answer(t("bad_age", lang))
        return
    profile.age = age
    await session.commit()
    await state.set_state(ProfileStates.gender)
    await message.answer(t("ask_gender", lang), reply_markup=gender_kb(lang))


@router.callback_query(ProfileStates.age, F.data == "keep:age")
async def keep_age(callback: CallbackQuery, session: AsyncSession, state: FSMContext) -> None:
    ctx = await callback_context(callback, session)
    if ctx is None:
        return
    user, cb_message = ctx
    await callback.answer()
    await state.set_state(ProfileStates.gender)
    await cb_message.answer(t("ask_gender", user.language), reply_markup=gender_kb(user.language))


@router.callback_query(ProfileStates.gender, F.data.startswith("gender:"))
async def set_gender(callback: CallbackQuery, session: AsyncSession, state: FSMContext) -> None:
    owner = await _profile_owner_cb(callback, session, state)
    if owner is None:
        return
    user, profile, cb_message = owner
    gender = (callback.data or "").split(":")[1]
    profile.gender = Gender(gender)
    await session.commit()
    await callback.answer()
    await state.set_state(ProfileStates.looking_for)
    await cb_message.edit_text(t("ask_looking", user.language), reply_markup=looking_kb(user.language))


@router.callback_query(ProfileStates.looking_for, F.data.startswith("looking:"))
async def set_looking(callback: CallbackQuery, session: AsyncSession, state: FSMContext) -> None:
    owner = await _profile_owner_cb(callback, session, state)
    if owner is None:
        return
    user, profile, cb_message = owner
    looking = (callback.data or "").split(":")[1]
    profile.looking_for = LookingFor(looking)
    await session.commit()
    await callback.answer()
    await state.set_state(ProfileStates.location)
    reply, inline = location_kb(user.language, has_current=profile.lat is not None)
    await cb_message.answer(t("ask_location", user.language), reply_markup=reply)
    await cb_message.answer(t("location_more", user.language), reply_markup=inline)


@router.message(ProfileStates.location, F.location)
async def set_location(message: Message, session: AsyncSession, state: FSMContext) -> None:
    owner = await _profile_owner(message, session, state)
    if owner is None or message.location is None:
        return
    user, profile = owner
    profile.lat = message.location.latitude
    profile.lon = message.location.longitude
    profile.city_name = await reverse_geocode(profile.lat, profile.lon)
    await session.commit()
    await drop_reply_keyboard(message)
    await state.set_state(ProfileStates.name)
    kb = keep_kb(user.language, "keep:name") if profile.name else None
    await message.answer(t("ask_name", user.language), reply_markup=kb)


@router.callback_query(ProfileStates.location, F.data == "loc:text")
@router.callback_query(ProfileStates.location_text, F.data == "loc:text")
@router.callback_query(ProfileStates.location_confirm, F.data == "loc:text")
async def location_text_start(
    callback: CallbackQuery, session: AsyncSession, state: FSMContext
) -> None:
    ctx = await callback_context(callback, session)
    if ctx is None:
        return
    user, cb_message = ctx
    await callback.answer()
    await state.set_state(ProfileStates.location_text)
    await cb_message.answer(
        t("ask_location_text", user.language),
        reply_markup=ReplyKeyboardRemove(),
    )


@router.message(ProfileStates.location_text, F.location)
async def location_text_got_gps(
    message: Message, session: AsyncSession, state: FSMContext
) -> None:
    await set_location(message, session, state)


@router.message(ProfileStates.location_text, F.text)
async def location_text_search(
    message: Message, session: AsyncSession, state: FSMContext
) -> None:
    owner = await _profile_owner(message, session, state)
    if owner is None:
        return
    user, profile = owner
    lang = user.language or "ru"
    hits = await search_settlements(session, message.text or "")
    if not hits:
        reply, inline = location_kb(lang, has_current=profile.lat is not None)
        await message.answer(t("location_not_found", lang), reply_markup=reply)
        await message.answer(t("location_more", lang), reply_markup=inline)
        await state.set_state(ProfileStates.location)
        return

    choices = disambiguation_choices(hits)
    if len(choices) > 1:
        buttons: list[tuple[int, str]] = []
        for hit in choices:
            buttons.append((hit.id, await choice_button_label(session, hit)))
        await state.set_state(ProfileStates.location_confirm)
        await message.answer(
            t("location_pick_title", lang),
            reply_markup=location_pick_kb(lang, buttons),
        )
        return

    hit = choices[0]
    neighbours = await nearest_settlements(
        session, hit.lat, hit.lon, exclude_id=hit.id, limit=2
    )
    await state.update_data(pending_settlement_id=hit.id)
    await state.set_state(ProfileStates.location_confirm)
    await message.answer(
        format_confirm(hit, neighbours, lang),
        reply_markup=location_confirm_kb(lang),
    )


async def _apply_settlement_and_continue(
    callback: CallbackQuery,
    session: AsyncSession,
    state: FSMContext,
    settlement_id: int,
) -> None:
    owner = await _profile_owner_cb(callback, session, state)
    if owner is None:
        return
    user, profile, cb_message = owner
    place = await get_settlement(session, settlement_id)
    if place is None:
        await callback.answer(t("location_not_found", user.language), show_alert=True)
        await state.set_state(ProfileStates.location_text)
        return
    profile.lat = place.lat
    profile.lon = place.lon
    profile.city_name = place.display_name[:128]
    await session.commit()
    await callback.answer()
    try:
        await cb_message.edit_reply_markup(reply_markup=None)
    except Exception:
        pass
    await state.set_state(ProfileStates.name)
    kb = keep_kb(user.language, "keep:name") if profile.name else None
    await cb_message.answer(t("ask_name", user.language), reply_markup=kb)


@router.callback_query(ProfileStates.location_confirm, F.data.startswith("loc:pick:"))
async def location_pick(
    callback: CallbackQuery, session: AsyncSession, state: FSMContext
) -> None:
    sid = int((callback.data or "").split(":")[2])
    await _apply_settlement_and_continue(callback, session, state, sid)


@router.callback_query(ProfileStates.location_confirm, F.data == "loc:yes")
async def location_confirm_yes(
    callback: CallbackQuery, session: AsyncSession, state: FSMContext
) -> None:
    data = await state.get_data()
    sid = data.get("pending_settlement_id")
    if not sid:
        await callback.answer(t("location_not_found", "ru"), show_alert=True)
        return
    await _apply_settlement_and_continue(callback, session, state, int(sid))


@router.callback_query(ProfileStates.location_confirm, F.data == "loc:no")
async def location_confirm_no(
    callback: CallbackQuery, session: AsyncSession, state: FSMContext
) -> None:
    ctx = await callback_context(callback, session)
    if ctx is None:
        return
    user, cb_message = ctx
    await callback.answer()
    try:
        await cb_message.edit_reply_markup(reply_markup=None)
    except Exception:
        pass
    await state.set_state(ProfileStates.location_text)
    await cb_message.answer(t("ask_location_text", user.language))


@router.callback_query(ProfileStates.location, F.data == "keep:location")
async def keep_location(callback: CallbackQuery, session: AsyncSession, state: FSMContext) -> None:
    ctx = await callback_context(callback, session)
    if ctx is None:
        return
    user, cb_message = ctx
    await callback.answer()
    await drop_reply_keyboard(cb_message)
    await state.set_state(ProfileStates.name)
    kb = keep_kb(user.language, "keep:name") if user.profile and user.profile.name else None
    await cb_message.answer(t("ask_name", user.language), reply_markup=kb)


@router.message(ProfileStates.name)
async def set_name(message: Message, session: AsyncSession, state: FSMContext) -> None:
    owner = await _profile_owner(message, session, state)
    if owner is None:
        return
    user, profile = owner
    name = (message.text or "").strip()
    if not 2 <= len(name) <= 32:
        await message.answer(t("bad_name", user.language))
        return
    profile.name = name
    await session.commit()
    await state.set_state(ProfileStates.about)
    await message.answer(
        t("ask_about", user.language),
        reply_markup=about_kb(user.language, has_current=bool(profile.description)),
    )


@router.callback_query(ProfileStates.name, F.data == "keep:name")
async def keep_name(callback: CallbackQuery, session: AsyncSession, state: FSMContext) -> None:
    owner = await _profile_owner_cb(callback, session, state)
    if owner is None:
        return
    user, profile, cb_message = owner
    await callback.answer()
    await state.set_state(ProfileStates.about)
    await cb_message.answer(
        t("ask_about", user.language),
        reply_markup=about_kb(user.language, has_current=bool(profile.description)),
    )


@router.message(ProfileStates.about)
async def set_about(message: Message, session: AsyncSession, state: FSMContext) -> None:
    owner = await _profile_owner(message, session, state)
    if owner is None:
        return
    user, profile = owner
    profile.description = (message.text or "").strip()[:900]
    await session.commit()
    await _ask_photo(message, state, user)


@router.callback_query(ProfileStates.about, F.data.in_({"about:skip", "keep:about"}))
async def about_skip_or_keep(
    callback: CallbackQuery, session: AsyncSession, state: FSMContext
) -> None:
    owner = await _profile_owner_cb(callback, session, state)
    if owner is None:
        return
    user, profile, cb_message = owner
    if callback.data == "about:skip":
        profile.description = None
        await session.commit()
    await callback.answer()
    await _ask_photo(cb_message, state, user)


async def _ask_photo(message: Message, state: FSMContext, user: User) -> None:
    await state.set_state(ProfileStates.photo)
    await state.update_data(draft_photos=[])
    can_keep = bool(profile_photo_ids(user.profile))
    await message.answer(
        t("ask_photo", user.language, n=0, max=MAX_PROFILE_PHOTOS),
        reply_markup=photo_step_kb(user.language, 0, can_keep=can_keep),
    )


async def _finish_photos(
    message: Message,
    session: AsyncSession,
    state: FSMContext,
    user: User,
    photos: list[str],
    *,
    tg_username: str | None,
) -> None:
    if user.profile is None:
        await state.clear()
        return
    if not photos:
        await message.answer(t("photo_required", user.language))
        return
    set_profile_photos(user.profile, photos)
    user.profile.is_complete = True
    user.profile.is_active = True
    if tg_username:
        user.username = tg_username
    await session.commit()
    await state.clear()
    fresh = await load_user(session, user.tg_id)
    if fresh is None:
        return
    await after_profile_ready(message, session, fresh, state)


async def _extract_photo_id(
    message: Message, bot: Bot, lang: str, *, from_document: bool
) -> str | None:
    if from_document and message.document:
        mime = message.document.mime_type or ""
        name = (message.document.file_name or "").lower()
        ok_mime = mime in {"image/jpeg", "image/png", "image/gif"}
        ok_ext = name.endswith((".jpg", ".jpeg", ".png", ".gif"))
        if not (ok_mime or ok_ext):
            await message.answer(t("bad_photo", lang))
            return None
        if message.document.file_size and message.document.file_size > MAX_PHOTO_BYTES:
            await message.answer(t("bad_photo", lang))
            return None
        return message.document.file_id
    if message.photo:
        photo = message.photo[-1]
        try:
            f = await bot.get_file(photo.file_id)
            if f.file_size and f.file_size > MAX_PHOTO_BYTES:
                await message.answer(t("bad_photo", lang))
                return None
        except Exception:
            pass
        return photo.file_id
    await message.answer(t("bad_photo", lang))
    return None


@router.message(ProfileStates.photo, F.photo | F.document)
async def set_photo(message: Message, session: AsyncSession, state: FSMContext, bot: Bot) -> None:
    if message.from_user is None:
        return
    owner = await _profile_owner(message, session, state)
    if owner is None:
        return
    user, _ = owner
    lang = user.language or "ru"
    file_id = await _extract_photo_id(
        message, bot, lang, from_document=message.document is not None
    )
    if not file_id:
        return

    async with _photo_draft_lock(message.from_user.id):
        data = await state.get_data()
        draft: list[str] = list(data.get("draft_photos") or [])
        if len(draft) >= MAX_PROFILE_PHOTOS:
            await message.answer(t("photo_full", lang))
            await _finish_photos(
                message,
                session,
                state,
                user,
                draft,
                tg_username=message.from_user.username,
            )
            return
        draft.append(file_id)
        await state.update_data(draft_photos=draft)
        if len(draft) >= MAX_PROFILE_PHOTOS:
            await _finish_photos(
                message,
                session,
                state,
                user,
                draft,
                tg_username=message.from_user.username,
            )
            return
        await message.answer(
            t("ask_photo_more", lang, n=len(draft), max=MAX_PROFILE_PHOTOS),
            reply_markup=photo_step_kb(lang, len(draft), can_keep=False),
        )


@router.callback_query(ProfileStates.photo, F.data == "photo:done")
async def photo_done(callback: CallbackQuery, session: AsyncSession, state: FSMContext) -> None:
    owner = await _profile_owner_cb(callback, session, state)
    if owner is None:
        return
    user, _, cb_message = owner
    async with _photo_draft_lock(callback.from_user.id):
        data = await state.get_data()
        draft: list[str] = list(data.get("draft_photos") or [])
        if not draft:
            await callback.answer(t("photo_required", user.language), show_alert=True)
            return
        await callback.answer()
        try:
            await cb_message.edit_reply_markup(reply_markup=None)
        except Exception:
            pass
        await _finish_photos(
            cb_message,
            session,
            state,
            user,
            draft,
            tg_username=callback.from_user.username,
        )


@router.callback_query(ProfileStates.photo, F.data == "keep:photo")
async def keep_photo(callback: CallbackQuery, session: AsyncSession, state: FSMContext) -> None:
    owner = await _profile_owner_cb(callback, session, state)
    if owner is None:
        return
    user, profile, cb_message = owner
    existing = profile_photo_ids(profile)
    await callback.answer()
    await _finish_photos(
        cb_message,
        session,
        state,
        user,
        existing,
        tg_username=callback.from_user.username,
    )


async def _finish_edit_photos(
    message: Message,
    session: AsyncSession,
    state: FSMContext,
    user: User,
    draft: list[str],
) -> None:
    if user.profile is None:
        await state.clear()
        return
    if not draft:
        await message.answer(t("photo_required", user.language))
        return
    set_profile_photos(user.profile, draft)
    await session.commit()
    await state.clear()
    await show_my_profile(message, user, user.profile)


@router.message(ProfileStates.edit_photo, F.photo | F.document)
async def edit_photo(message: Message, session: AsyncSession, state: FSMContext, bot: Bot) -> None:
    if message.from_user is None:
        return
    owner = await _profile_owner(message, session, state)
    if owner is None:
        return
    user, _ = owner
    lang = user.language or "ru"
    file_id = await _extract_photo_id(
        message, bot, lang, from_document=message.document is not None
    )
    if not file_id:
        return

    async with _photo_draft_lock(message.from_user.id):
        data = await state.get_data()
        draft: list[str] = list(data.get("draft_photos") or [])
        if len(draft) >= MAX_PROFILE_PHOTOS:
            await _finish_edit_photos(message, session, state, user, draft)
            return
        draft.append(file_id)
        await state.update_data(draft_photos=draft)
        if len(draft) >= MAX_PROFILE_PHOTOS:
            await _finish_edit_photos(message, session, state, user, draft)
            return
        await message.answer(
            t("ask_photo_more", lang, n=len(draft), max=MAX_PROFILE_PHOTOS),
            reply_markup=photo_step_kb(lang, len(draft), can_keep=False),
        )


@router.callback_query(ProfileStates.edit_photo, F.data == "photo:done")
async def edit_photo_done(
    callback: CallbackQuery, session: AsyncSession, state: FSMContext
) -> None:
    owner = await _profile_owner_cb(callback, session, state)
    if owner is None:
        return
    user, _, cb_message = owner
    async with _photo_draft_lock(callback.from_user.id):
        data = await state.get_data()
        draft: list[str] = list(data.get("draft_photos") or [])
        if not draft:
            await callback.answer(t("photo_required", user.language), show_alert=True)
            return
        await callback.answer()
        await _finish_edit_photos(cb_message, session, state, user, draft)


@router.callback_query(ProfileStates.edit_photo, F.data == "keep:photo")
async def edit_photo_keep(
    callback: CallbackQuery, session: AsyncSession, state: FSMContext
) -> None:
    owner = await _profile_owner_cb(callback, session, state)
    if owner is None:
        return
    user, profile, cb_message = owner
    await state.clear()
    await callback.answer()
    await show_my_profile(cb_message, user, profile)


@router.message(ProfileStates.edit_text)
async def edit_text(message: Message, session: AsyncSession, state: FSMContext) -> None:
    owner = await _profile_owner(message, session, state)
    if owner is None:
        return
    user, profile = owner
    profile.description = (message.text or "").strip()[:900]
    await session.commit()
    await state.clear()
    await show_my_profile(message, user, profile)
