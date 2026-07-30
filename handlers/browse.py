from __future__ import annotations

from aiogram import Bot, F, Router
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message, ReplyKeyboardRemove
from sqlalchemy.ext.asyncio import AsyncSession

from database.models import LikeAction, User
from handlers.common import blocked_text, callback_context, load_user, message_user, show_main_menu
from keyboards.inline import (
    browse_reply_kb,
    channels_kb,
    main_menu_kb,
    message_compose_kb,
    premium_cta_kb,
    profile_enable_kb,
)
from locales import t
from services.activity import touch_activity
from services.browse import next_profile, profile_caption
from services.channels import format_channels_lines, list_active_channels, user_subscribed_all
from services.media import as_photo_input, media_photos_for_profile, profile_photo_ids
from services.likes import (
    deliver_like_media,
    empty_message_payload,
    format_likes_list,
    list_unseen_likers,
    mark_likes_seen,
    notify_like_batch,
    record_action,
)
from services.limits import can_browse
from services.reports import file_report
from services.settings_service import is_registration_only
from states.browse import BrowseStates, MessageStates

router = Router()


def _limit_promo_text(lang: str) -> str:
    return f"{t('limit_reached', lang)}\n\n{t('premium_benefits', lang)}"


def _is_btn(text: str | None, key: str, lang: str) -> bool:
    if not text:
        return False
    return text in {t(key, lang), t(key, "ru"), t(key, "tg")}


async def _say_limit(message: Message, lang: str) -> None:
    body = _limit_promo_text(lang)
    await message.answer(".", reply_markup=ReplyKeyboardRemove())
    await message.answer(body, reply_markup=premium_cta_kb(lang, with_main_menu=True))


async def _guard_feed_user(
    message: Message,
    session: AsyncSession,
    user: User,
    state: FSMContext,
    lang: str,
) -> bool:
    """False if the user must not interact with the feed."""
    if user.is_blocked:
        await state.clear()
        await message.answer(
            await blocked_text(session, lang), reply_markup=ReplyKeyboardRemove()
        )
        return False
    if await is_registration_only(session):
        await state.clear()
        await message.answer(t("soft_launch", lang), reply_markup=ReplyKeyboardRemove())
        await show_main_menu(message, user)
        return False
    return True


async def _send_profile_card(
    dest: Bot, chat_id: int, profile, caption: str, kb, lang: str
) -> None:
    photos = profile_photo_ids(profile)
    if not photos:
        await dest.send_message(chat_id, caption, reply_markup=kb)
        return
    if len(photos) == 1:
        photo = as_photo_input(photos[0])
        if photo is not None:
            await dest.send_photo(chat_id, photo, caption=caption, reply_markup=kb)
        else:
            await dest.send_message(chat_id, caption, reply_markup=kb)
        return
    media = media_photos_for_profile(profile, caption=caption)
    if not media:
        await dest.send_message(chat_id, caption, reply_markup=kb)
        return
    await dest.send_media_group(chat_id, media)
    await dest.send_message(chat_id, t("browse_hint", lang), reply_markup=kb)


async def start_browse(
    message: Message,
    session: AsyncSession,
    user: User,
    bot: Bot | None = None,
    state: FSMContext | None = None,
) -> None:
    # After record_action / notify commits the caller's User may be expired —
    # never touch user.profile via lazy load (MissingGreenlet).
    fresh = await load_user(session, user.tg_id)
    if fresh is None:
        return
    user = fresh
    lang = user.language or "ru"
    dest = bot or message.bot
    chat_id = user.tg_id

    async def say(text: str, **kwargs) -> None:
        await dest.send_message(chat_id, text, **kwargs)

    if user.is_blocked:
        if state:
            await state.clear()
        await say(
            await blocked_text(session, lang), reply_markup=ReplyKeyboardRemove()
        )
        return
    if await is_registration_only(session):
        if state:
            await state.clear()
        await say(t("soft_launch", lang), reply_markup=ReplyKeyboardRemove())
        await say("☰", reply_markup=main_menu_kb(lang))
        return
    if not await user_subscribed_all(dest, session, user):
        if state:
            await state.clear()
        channels = await list_active_channels(session)
        body = t("need_channels", lang, channels=format_channels_lines(channels))
        await say(".", reply_markup=ReplyKeyboardRemove())
        await say(body, reply_markup=channels_kb(lang, channels))
        return
    if not await can_browse(session, user, user.profile):
        if state:
            await state.clear()
        body = _limit_promo_text(lang)
        await say(".", reply_markup=ReplyKeyboardRemove())
        await say(body, reply_markup=premium_cta_kb(lang, with_main_menu=True))
        return
    if not user.profile or not user.profile.is_complete:
        from handlers.profile import begin_profile_flow

        if state is None:
            await say(t("ask_age", lang), reply_markup=main_menu_kb(lang))
            return
        await begin_profile_flow(message, session, state, user, refill=False)
        return
    if not user.profile.is_active:
        # Switched off by the user ("Больше не ищу") or by an admin — ask first.
        if state:
            await state.clear()
        await say(".", reply_markup=ReplyKeyboardRemove())
        await say(t("profile_hidden", lang), reply_markup=profile_enable_kb(lang))
        return

    await touch_activity(session, user)

    profile = await next_profile(session, user, user.profile)
    if profile is None:
        if state:
            await state.clear()
        await say(t("empty_feed", lang), reply_markup=ReplyKeyboardRemove())
        await say("☰", reply_markup=main_menu_kb(lang))
        return

    if state:
        await state.set_state(BrowseStates.viewing)
        await state.update_data(browse_target=profile.user_id)

    caption = profile_caption(profile)
    kb = browse_reply_kb(lang)
    await _send_profile_card(dest, chat_id, profile, caption, kb, lang)


@router.callback_query(F.data == "browse:start")
async def cb_browse(
    callback: CallbackQuery, session: AsyncSession, bot: Bot, state: FSMContext
) -> None:
    ctx = await callback_context(callback, session)
    if ctx is None:
        return
    user, message = ctx
    await callback.answer()
    await start_browse(message, session, user, bot, state)


@router.callback_query(F.data == "profile:enable")
async def cb_profile_enable(
    callback: CallbackQuery, session: AsyncSession, bot: Bot, state: FSMContext
) -> None:
    ctx = await callback_context(callback, session)
    if ctx is None:
        return
    user, message = ctx
    await callback.answer()
    if not user.is_blocked and user.profile and not user.profile.is_active:
        user.profile.is_active = True
        await session.commit()
    await start_browse(message, session, user, bot, state)


@router.callback_query(F.data == "channels:check")
async def cb_channels(
    callback: CallbackQuery, session: AsyncSession, bot: Bot, state: FSMContext
) -> None:
    ctx = await callback_context(callback, session)
    if ctx is None:
        return
    user, message = ctx
    if await user_subscribed_all(bot, session, user):
        await callback.answer(t("channels_ok", user.language))
        await start_browse(message, session, user, bot, state)
    else:
        await callback.answer(t("channels_fail", user.language), show_alert=True)


async def _rate_from_message(
    message: Message,
    session: AsyncSession,
    bot: Bot,
    state: FSMContext,
    action: LikeAction,
) -> None:
    user = await message_user(message, session)
    if user is None or user.profile is None:
        await state.clear()
        return
    lang = user.language or "ru"
    data = await state.get_data()
    target_id = data.get("browse_target")
    if not target_id:
        await state.clear()
        await show_main_menu(message, user)
        return

    if not await _guard_feed_user(message, session, user, state, lang):
        return

    if not await can_browse(session, user, user.profile):
        await state.clear()
        await _say_limit(message, lang)
        return

    try:
        like = await record_action(session, user, user.profile, int(target_id), action)
    except PermissionError as exc:
        await state.clear()
        if exc.args and exc.args[0] == "blocked":
            await message.answer(
                await blocked_text(session, lang), reply_markup=ReplyKeyboardRemove()
            )
        else:
            await _say_limit(message, lang)
        return

    if like and action in (LikeAction.like, LikeAction.message):
        await notify_like_batch(bot, session, int(target_id))
    if like and action == LikeAction.like:
        await bot.send_message(user.tg_id, t("like_sent", lang))

    await start_browse(message, session, user, bot, state)


@router.message(BrowseStates.viewing, F.text)
async def browse_reply_action(
    message: Message, session: AsyncSession, bot: Bot, state: FSMContext
) -> None:
    user = await message_user(message, session)
    if user is None:
        return
    lang = user.language or "ru"
    text = message.text or ""

    if _is_btn(text, "btn_like", lang):
        await _rate_from_message(message, session, bot, state, LikeAction.like)
        return
    if _is_btn(text, "btn_dislike", lang):
        await _rate_from_message(message, session, bot, state, LikeAction.dislike)
        return
    if _is_btn(text, "btn_message", lang):
        await _start_message_flow(message, session, state, user)
        return
    if _is_btn(text, "btn_report", lang):
        await _report_from_message(message, session, bot, state, user)
        return
    if _is_btn(text, "btn_premium", lang):
        from handlers.premium import _send_premium_menu

        await _send_premium_menu(message, session, user)
        return
    if _is_btn(text, "btn_sleep", lang):
        await state.clear()
        await message.answer("🚪", reply_markup=ReplyKeyboardRemove())
        await show_main_menu(message, user)
        return

    await message.answer(t("browse_hint", lang), reply_markup=browse_reply_kb(lang))


async def _start_message_flow(
    message: Message, session: AsyncSession, state: FSMContext, user: User
) -> None:
    lang = user.language or "ru"
    if not await _guard_feed_user(message, session, user, state, lang):
        return
    if not await can_browse(session, user, user.profile):
        await state.clear()
        await _say_limit(message, lang)
        return
    data = await state.get_data()
    target_id = data.get("browse_target")
    if not target_id:
        await state.clear()
        await show_main_menu(message, user)
        return
    await state.set_state(MessageStates.content)
    await state.update_data(
        msg_target=int(target_id),
        msg_payload=empty_message_payload(),
    )
    await message.answer(t("ask_message", lang), reply_markup=ReplyKeyboardRemove())
    await message.answer(
        t("btn_cancel", lang),
        reply_markup=message_compose_kb(lang),
    )


async def _report_from_message(
    message: Message,
    session: AsyncSession,
    bot: Bot,
    state: FSMContext,
    user: User,
) -> None:
    lang = user.language or "ru"
    if user.is_blocked:
        await message.answer(await blocked_text(session, lang))
        return
    if not await can_browse(session, user, user.profile):
        await state.clear()
        await _say_limit(message, lang)
        return
    data = await state.get_data()
    target_id = data.get("browse_target")
    if not target_id:
        await state.clear()
        await show_main_menu(message, user)
        return
    created, just_blocked = await file_report(session, user.tg_id, int(target_id))
    if created:
        await message.answer(t("report_ok", lang))
        try:
            await record_action(
                session, user, user.profile, int(target_id), LikeAction.dislike
            )
        except Exception:
            pass
        if just_blocked:
            target = await load_user(session, int(target_id))
            if target and not target.is_test:
                try:
                    await bot.send_message(
                        int(target_id),
                        await blocked_text(session, target.language or "ru"),
                    )
                except Exception:
                    pass
    else:
        await message.answer(t("report_dup", lang))
    await start_browse(message, session, user, bot, state)


async def _finalize_message(
    message: Message,
    session: AsyncSession,
    bot: Bot,
    state: FSMContext,
    payload: dict,
) -> None:
    user = await message_user(message, session)
    if user is None or user.profile is None:
        await state.clear()
        return
    lang = user.language or "ru"
    data = await state.get_data()
    target_id_raw = data.get("msg_target")
    if target_id_raw is None:
        await state.clear()
        await message.answer(t("msg_cancelled", lang), reply_markup=main_menu_kb(lang))
        return
    target_id = int(target_id_raw)
    if not await _guard_feed_user(message, session, user, state, lang):
        return
    try:
        like = await record_action(
            session,
            user,
            user.profile,
            target_id,
            LikeAction.message,
            message_payload=payload,
        )
    except PermissionError as exc:
        await state.clear()
        if exc.args and exc.args[0] == "blocked":
            await message.answer(
                await blocked_text(session, lang), reply_markup=ReplyKeyboardRemove()
            )
        else:
            await _say_limit(message, lang)
        return
    await state.clear()
    if like:
        await notify_like_batch(bot, session, target_id)
        await message.answer(t("message_sent", lang))
    await start_browse(message, session, user, bot, state)


@router.callback_query(MessageStates.content, F.data == "msg:cancel")
async def msg_content_cancel(
    callback: CallbackQuery, session: AsyncSession, state: FSMContext
) -> None:
    ctx = await callback_context(callback, session)
    if ctx is None:
        return
    user, cb_message = ctx
    lang = user.language or "ru"
    data = await state.get_data()
    browse_target = data.get("browse_target")
    await state.clear()
    await state.set_state(BrowseStates.viewing)
    if browse_target is not None:
        await state.update_data(browse_target=browse_target)
    await callback.answer()
    try:
        await cb_message.edit_reply_markup(reply_markup=None)
    except Exception:
        pass
    await cb_message.answer(
        t("msg_cancelled", lang),
        reply_markup=browse_reply_kb(lang),
    )


@router.message(MessageStates.content, F.voice)
async def msg_content_voice(
    message: Message, session: AsyncSession, state: FSMContext, bot: Bot
) -> None:
    if message.voice is None:
        return
    payload = empty_message_payload()
    payload["voice_file_id"] = message.voice.file_id
    await _finalize_message(message, session, bot, state, payload)


@router.message(MessageStates.content, F.video_note)
async def msg_content_video_note(
    message: Message, session: AsyncSession, state: FSMContext, bot: Bot
) -> None:
    if message.video_note is None:
        return
    payload = empty_message_payload()
    payload["video_note_file_id"] = message.video_note.file_id
    await _finalize_message(message, session, bot, state, payload)


@router.message(MessageStates.content, F.text)
async def msg_content_text(
    message: Message, session: AsyncSession, state: FSMContext, bot: Bot
) -> None:
    user = await message_user(message, session)
    if user is None:
        return
    lang = user.language or "ru"
    text = (message.text or "").strip()[:500]
    if not text:
        await message.answer(t("msg_need_content", lang))
        return
    payload = empty_message_payload()
    payload["text"] = text
    await _finalize_message(message, session, bot, state, payload)


@router.message(MessageStates.content)
async def msg_content_other(message: Message, session: AsyncSession) -> None:
    user = await message_user(message, session)
    if user is None:
        return
    await message.answer(t("msg_need_content", user.language or "ru"))


@router.callback_query(F.data == "likes:view")
async def cb_view_likes(
    callback: CallbackQuery, session: AsyncSession, bot: Bot
) -> None:
    ctx = await callback_context(callback, session)
    if ctx is None:
        return
    user, message = ctx
    rows = await list_unseen_likers(session, user.tg_id)
    text = format_likes_list(rows, user.language)
    likes = [like for _, _, like in rows]
    await callback.answer()
    try:
        await message.edit_reply_markup(reply_markup=None)
    except Exception:
        pass
    await message.answer(
        text,
        parse_mode="HTML",
        disable_web_page_preview=True,
        reply_markup=main_menu_kb(user.language),
    )
    for like in likes:
        await deliver_like_media(bot, user.tg_id, like)
    await mark_likes_seen(session, user.tg_id)
