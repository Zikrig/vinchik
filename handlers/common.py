from __future__ import annotations

import asyncio

from aiogram.exceptions import TelegramBadRequest
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message
from aiogram.types import ReplyKeyboardRemove
from sqlalchemy.ext.asyncio import AsyncSession

from database.models import Profile, User
from keyboards.inline import main_menu_kb, my_profile_kb, premium_cta_kb
from locales import t
from services.browse import profile_caption
from services.media import as_photo_input
from services.settings_service import get_support_contact, is_registration_only
from services.users import is_premium

# Pause after premium promo before the first feed card (post-registration).
_POST_PROMO_FEED_DELAY_SEC = 5.0


async def blocked_text(session: AsyncSession, lang: str | None) -> str:
    support = await get_support_contact(session)
    return t("you_are_blocked", lang or "ru", support=support)


async def load_user(session: AsyncSession, tg_id: int) -> User | None:
    from services.users import load_user_with_profile

    return await load_user_with_profile(session, tg_id)


async def ensure_user(session: AsyncSession, tg_id: int, username: str | None) -> User:
    """Row for a known chat; recreates it if the account was wiped meanwhile."""
    from services.users import get_or_create_user

    user = await load_user(session, tg_id)
    if user is not None:
        return user
    return await get_or_create_user(session, tg_id, username)


async def message_user(message: Message, session: AsyncSession) -> User | None:
    """Author of a private message; ``None`` for channel posts and anonymous senders."""
    if message.from_user is None:
        return None
    return await ensure_user(session, message.from_user.id, message.from_user.username)


async def callback_context(
    callback: CallbackQuery, session: AsyncSession
) -> tuple[User, Message] | None:
    """User + the message the button lives on.

    ``None`` means the click cannot be served: Telegram drops the message body
    for buttons older than 48h (``InaccessibleMessage`` has no text or author).
    """
    message = callback.message
    if not isinstance(message, Message):
        user = await load_user(session, callback.from_user.id)
        lang = (user.language if user else None) or "ru"
        await callback.answer(t("stale_button", lang), show_alert=True)
        return None
    user = await ensure_user(session, callback.from_user.id, callback.from_user.username)
    return user, message


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
            try:
                await message.answer_photo(photo, caption=caption, reply_markup=kb)
                return
            except TelegramBadRequest:
                # Stale file_id (bot recreated / token changed) or bad local path.
                pass
        await message.answer(caption, reply_markup=kb)
        return
    media = media_photos_for_profile(profile, caption=caption)
    if media:
        try:
            await message.answer_media_group(media)
            await message.answer(t("my_profile_title", lang), reply_markup=kb)
            return
        except TelegramBadRequest:
            pass
    await message.answer(caption, reply_markup=kb)


async def show_main_menu(message: Message, user: User) -> None:
    await message.answer(
        t("main_menu_title", user.language or "ru"),
        reply_markup=main_menu_kb(user.language),
    )


async def drop_reply_keyboard(message: Message) -> None:
    """Remove reply keyboard without leaving a visible '.' / junk message."""
    rm = await message.answer("\u2060", reply_markup=ReplyKeyboardRemove())
    try:
        await rm.delete()
    except Exception:
        pass


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
    await drop_reply_keyboard(message)
    if not is_premium(user):
        await message.answer(
            t("premium_promo", lang),
            reply_markup=premium_cta_kb(lang),
        )
        await asyncio.sleep(_POST_PROMO_FEED_DELAY_SEC)
    await start_browse(message, session, user, state=state)
