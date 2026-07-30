from aiogram import F, Router
from aiogram.exceptions import TelegramBadRequest
from aiogram.filters import CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message
from sqlalchemy.ext.asyncio import AsyncSession

from handlers.common import callback_context, show_main_menu
from keyboards.inline import language_kb
from locales import t
from services.media import as_photo_input
from services.settings_service import get_welcome_post, welcome_post_configured
from services.users import get_or_create_user, set_language
from states.profile import ProfileStates

router = Router()


async def _send_start_welcome(message: Message, session: AsyncSession, user) -> None:
    """Welcome post with photo from admin settings, or locale text fallback."""
    post = await get_welcome_post(session)
    if welcome_post_configured(post):
        photo = as_photo_input(post["photo_file_id"])
        caption = (post.get("text") or "").strip() or None
        if photo is not None:
            try:
                await message.answer_photo(photo, caption=caption)
                return
            except TelegramBadRequest:
                pass
        if caption:
            await message.answer(caption)
            return

    if user.language_chosen:
        await message.answer(t("welcome", user.language or "ru"))
    else:
        await message.answer(t("welcome_bilingual", "ru"))


async def _continue_after_language(
    message: Message,
    session: AsyncSession,
    state: FSMContext,
    user,
    *,
    tg_username: str | None,
) -> None:
    lang = user.language or "ru"
    if user.profile and user.profile.is_complete:
        await show_main_menu(message, user, with_help=True)
        return

    if not tg_username:
        await state.set_state(ProfileStates.waiting_username)
        await message.answer(t("need_username", lang))
        return

    from handlers.profile import begin_profile_flow

    await begin_profile_flow(message, session, state, user, refill=False)


@router.message(CommandStart())
async def cmd_start(message: Message, session: AsyncSession, state: FSMContext) -> None:
    await state.clear()
    if message.from_user is None:
        return
    user = await get_or_create_user(
        session,
        message.from_user.id,
        message.from_user.username,
    )
    if user.is_blocked:
        from handlers.common import blocked_text

        await message.answer(await blocked_text(session, user.language or "ru"))
        return

    # Complete profiles already use a language; treat as chosen for legacy rows.
    if user.profile and user.profile.is_complete and not user.language_chosen:
        user.language_chosen = True
        await session.commit()

    await _send_start_welcome(message, session, user)

    if user.profile and user.profile.is_complete:
        await show_main_menu(message, user, with_help=True)
        return

    if not user.language_chosen:
        await message.answer(t("choose_language", "ru"), reply_markup=language_kb())
        return

    await _continue_after_language(
        message, session, state, user, tg_username=message.from_user.username
    )


@router.callback_query(F.data.startswith("lang:"))
async def on_language(callback: CallbackQuery, session: AsyncSession, state: FSMContext) -> None:
    lang = (callback.data or "").split(":", 1)[1]
    if lang not in {"ru", "tg"}:
        await callback.answer()
        return
    ctx = await callback_context(callback, session)
    if ctx is None:
        return
    user, message = ctx
    await set_language(session, user, lang)
    await state.clear()
    await callback.answer()
    try:
        await message.edit_text(t("language_saved", lang))
    except TelegramBadRequest:
        await message.answer(t("language_saved", lang))

    await _continue_after_language(
        message,
        session,
        state,
        user,
        tg_username=callback.from_user.username,
    )
