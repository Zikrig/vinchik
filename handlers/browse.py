from __future__ import annotations

from aiogram import Bot, F, Router
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message, ReplyKeyboardRemove
from sqlalchemy.ext.asyncio import AsyncSession

from database.models import LikeAction, User
from handlers.common import load_user, show_main_menu
from keyboards.inline import (
    browse_reply_kb,
    channels_kb,
    main_menu_kb,
    msg_next_kb,
    premium_cta_kb,
)
from locales import t
from services.activity import touch_activity
from services.browse import next_profile, profile_caption
from services.channels import format_channels_lines, list_active_channels, user_subscribed_all
from services.media import as_photo_input
from services.likes import (
    MAX_MESSAGE_ATTACH_BYTES,
    MAX_MESSAGE_ATTACHMENTS,
    deliver_like_media,
    empty_message_payload,
    format_likes_list,
    list_unseen_likers,
    mark_likes_seen,
    notify_like_batch,
    record_action,
)
from services.limits import can_browse, can_like
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


def _attach_left_mb(used: int) -> str:
    left = max(0, MAX_MESSAGE_ATTACH_BYTES - used)
    return f"{left / (1024 * 1024):.1f}".rstrip("0").rstrip(".")


def _attach_prompt(lang: str, n: int, used_bytes: int) -> str:
    return t(
        "ask_message_attach",
        lang,
        n=n,
        left_mb=_attach_left_mb(used_bytes),
    )


async def _say_limit(message: Message, lang: str) -> None:
    body = _limit_promo_text(lang)
    await message.answer(body, reply_markup=ReplyKeyboardRemove())
    await message.answer(body, reply_markup=premium_cta_kb(lang, with_main_menu=True))


async def start_browse(
    message: Message,
    session: AsyncSession,
    user: User,
    bot: Bot | None = None,
    state: FSMContext | None = None,
) -> None:
    lang = user.language or "ru"
    dest = bot or message.bot
    chat_id = user.tg_id

    async def say(text: str, **kwargs) -> None:
        await dest.send_message(chat_id, text, **kwargs)

    if user.is_blocked:
        if state:
            await state.clear()
        await say(t("you_are_blocked", lang), reply_markup=ReplyKeyboardRemove())
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
        if state:
            await state.clear()
        await say(t("ask_age", lang), reply_markup=main_menu_kb(lang))
        return
    if not user.profile.is_active:
        user.profile.is_active = True
        await session.commit()

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
    photo = as_photo_input(profile.photo_file_id)
    if photo is not None:
        await dest.send_photo(chat_id, photo, caption=caption, reply_markup=kb)
    else:
        await say(caption, reply_markup=kb)


@router.callback_query(F.data == "browse:start")
async def cb_browse(
    callback: CallbackQuery, session: AsyncSession, bot: Bot, state: FSMContext
) -> None:
    user = await load_user(session, callback.from_user.id)
    assert user and callback.message
    await callback.answer()
    if not await can_browse(session, user, user.profile):
        await state.clear()
        await bot.send_message(
            callback.from_user.id,
            _limit_promo_text(user.language or "ru"),
            reply_markup=premium_cta_kb(user.language or "ru", with_main_menu=True),
        )
        return
    await start_browse(callback.message, session, user, bot, state)


@router.callback_query(F.data == "channels:check")
async def cb_channels(
    callback: CallbackQuery, session: AsyncSession, bot: Bot, state: FSMContext
) -> None:
    user = await load_user(session, callback.from_user.id)
    assert user and callback.message
    if await user_subscribed_all(bot, session, user):
        await callback.answer(t("channels_ok", user.language))
        await start_browse(callback.message, session, user, bot, state)
    else:
        await callback.answer(t("channels_fail", user.language), show_alert=True)


async def _rate_from_message(
    message: Message,
    session: AsyncSession,
    bot: Bot,
    state: FSMContext,
    action: LikeAction,
) -> None:
    user = await load_user(session, message.from_user.id)  # type: ignore[union-attr]
    assert user and user.profile
    lang = user.language or "ru"
    data = await state.get_data()
    target_id = data.get("browse_target")
    if not target_id:
        await state.clear()
        await show_main_menu(message, user)
        return

    if not await can_browse(session, user, user.profile):
        await state.clear()
        await _say_limit(message, lang)
        return

    try:
        like = await record_action(session, user, user.profile, int(target_id), action)
    except PermissionError:
        await state.clear()
        await _say_limit(message, lang)
        return

    if like and action in (LikeAction.like, LikeAction.message):
        await notify_like_batch(bot, session, int(target_id))
    if like and action == LikeAction.like:
        await bot.send_message(user.tg_id, t("like_sent", lang))

    if action in (LikeAction.like, LikeAction.message) and like is not None:
        if not await can_like(session, user, user.profile):
            await state.clear()
            await _say_limit(message, lang)
            return

    await start_browse(message, session, user, bot, state)


@router.message(BrowseStates.viewing, F.text)
async def browse_reply_action(
    message: Message, session: AsyncSession, bot: Bot, state: FSMContext
) -> None:
    user = await load_user(session, message.from_user.id)  # type: ignore[union-attr]
    assert user
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
        msg_attach_bytes=0,
    )
    await message.answer(t("ask_message", lang), reply_markup=ReplyKeyboardRemove())


async def _report_from_message(
    message: Message,
    session: AsyncSession,
    bot: Bot,
    state: FSMContext,
    user: User,
) -> None:
    lang = user.language or "ru"
    if user.is_blocked:
        await message.answer(t("you_are_blocked", lang))
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
                        int(target_id), t("you_are_blocked", target.language or "ru")
                    )
                except Exception:
                    pass
    else:
        await message.answer(t("report_dup", lang))
    await start_browse(message, session, user, bot, state)


async def _enter_attachments(message: Message, state: FSMContext, lang: str) -> None:
    data = await state.get_data()
    payload = data.get("msg_payload") or empty_message_payload()
    used = int(data.get("msg_attach_bytes") or 0)
    atts = payload.get("attachments") or []
    await state.set_state(MessageStates.attachments)
    await message.answer(
        _attach_prompt(lang, len(atts), used),
        reply_markup=msg_next_kb(lang),
    )


@router.message(MessageStates.content, F.voice)
async def msg_content_voice(message: Message, session: AsyncSession, state: FSMContext) -> None:
    user = await load_user(session, message.from_user.id)  # type: ignore[union-attr]
    assert user and message.voice
    payload = (await state.get_data()).get("msg_payload") or empty_message_payload()
    payload["voice_file_id"] = message.voice.file_id
    payload["video_note_file_id"] = None
    await state.update_data(msg_payload=payload)
    await _enter_attachments(message, state, user.language or "ru")


@router.message(MessageStates.content, F.video_note)
async def msg_content_video_note(
    message: Message, session: AsyncSession, state: FSMContext
) -> None:
    user = await load_user(session, message.from_user.id)  # type: ignore[union-attr]
    assert user and message.video_note
    payload = (await state.get_data()).get("msg_payload") or empty_message_payload()
    payload["video_note_file_id"] = message.video_note.file_id
    payload["voice_file_id"] = None
    await state.update_data(msg_payload=payload)
    await _enter_attachments(message, state, user.language or "ru")


@router.message(MessageStates.content, F.text)
async def msg_content_text(message: Message, session: AsyncSession, state: FSMContext) -> None:
    user = await load_user(session, message.from_user.id)  # type: ignore[union-attr]
    assert user
    lang = user.language or "ru"
    text = (message.text or "").strip()[:500]
    if not text:
        await message.answer(t("msg_need_content", lang))
        return
    payload = (await state.get_data()).get("msg_payload") or empty_message_payload()
    payload["text"] = text
    await state.update_data(msg_payload=payload)
    await _enter_attachments(message, state, lang)


@router.message(MessageStates.content)
async def msg_content_other(message: Message, session: AsyncSession) -> None:
    user = await load_user(session, message.from_user.id)  # type: ignore[union-attr]
    assert user
    await message.answer(t("msg_need_content", user.language or "ru"))


async def _finalize_message(
    message: Message,
    session: AsyncSession,
    bot: Bot,
    state: FSMContext,
) -> None:
    user = await load_user(session, message.from_user.id)  # type: ignore[union-attr]
    assert user and user.profile
    lang = user.language or "ru"
    data = await state.get_data()
    target_id = int(data["msg_target"])
    payload = data.get("msg_payload") or empty_message_payload()
    try:
        like = await record_action(
            session,
            user,
            user.profile,
            target_id,
            LikeAction.message,
            message_payload=payload,
        )
    except PermissionError:
        await state.clear()
        await _say_limit(message, lang)
        return
    await state.clear()
    if like:
        await notify_like_batch(bot, session, target_id)
        await message.answer(t("message_sent", lang))
    await start_browse(message, session, user, bot, state)


async def _add_attachment(
    message: Message,
    session: AsyncSession,
    state: FSMContext,
    bot: Bot,
    *,
    file_id: str,
    size: int,
    kind: str,
) -> None:
    user = await load_user(session, message.from_user.id)  # type: ignore[union-attr]
    assert user
    lang = user.language or "ru"
    data = await state.get_data()
    payload = data.get("msg_payload") or empty_message_payload()
    atts: list = list(payload.get("attachments") or [])
    used = int(data.get("msg_attach_bytes") or 0)

    if len(atts) >= MAX_MESSAGE_ATTACHMENTS:
        await message.answer(
            t("msg_attach_full", lang),
            reply_markup=msg_next_kb(lang),
        )
        return

    if size <= 0:
        try:
            f = await bot.get_file(file_id)
            size = int(f.file_size or 0)
        except Exception:
            size = 0

    if used + size > MAX_MESSAGE_ATTACH_BYTES:
        await message.answer(
            t("msg_attach_too_big", lang),
            reply_markup=msg_next_kb(lang),
        )
        return

    atts.append({"type": kind, "file_id": file_id, "size": size})
    payload["attachments"] = atts
    used += size
    await state.update_data(msg_payload=payload, msg_attach_bytes=used)

    if len(atts) >= MAX_MESSAGE_ATTACHMENTS:
        await _finalize_message(message, session, bot, state)
        return

    await message.answer(
        _attach_prompt(lang, len(atts), used),
        reply_markup=msg_next_kb(lang),
    )


@router.message(MessageStates.attachments, F.photo)
async def msg_attach_photo(
    message: Message, session: AsyncSession, state: FSMContext, bot: Bot
) -> None:
    photo = message.photo[-1]  # type: ignore[index]
    await _add_attachment(
        message,
        session,
        state,
        bot,
        file_id=photo.file_id,
        size=int(photo.file_size or 0),
        kind="photo",
    )


@router.message(MessageStates.attachments, F.video)
async def msg_attach_video(
    message: Message, session: AsyncSession, state: FSMContext, bot: Bot
) -> None:
    assert message.video
    await _add_attachment(
        message,
        session,
        state,
        bot,
        file_id=message.video.file_id,
        size=int(message.video.file_size or 0),
        kind="video",
    )


@router.message(MessageStates.attachments, F.document)
async def msg_attach_document(
    message: Message, session: AsyncSession, state: FSMContext, bot: Bot
) -> None:
    assert message.document
    await _add_attachment(
        message,
        session,
        state,
        bot,
        file_id=message.document.file_id,
        size=int(message.document.file_size or 0),
        kind="document",
    )


@router.message(MessageStates.attachments)
async def msg_attach_other(message: Message, session: AsyncSession, state: FSMContext) -> None:
    user = await load_user(session, message.from_user.id)  # type: ignore[union-attr]
    assert user
    lang = user.language or "ru"
    data = await state.get_data()
    payload = data.get("msg_payload") or empty_message_payload()
    used = int(data.get("msg_attach_bytes") or 0)
    atts = payload.get("attachments") or []
    await message.answer(
        _attach_prompt(lang, len(atts), used),
        reply_markup=msg_next_kb(lang),
    )


@router.callback_query(MessageStates.attachments, F.data == "msg:next")
async def msg_next(
    callback: CallbackQuery, session: AsyncSession, bot: Bot, state: FSMContext
) -> None:
    assert callback.message
    await callback.answer()
    try:
        await callback.message.edit_reply_markup(reply_markup=None)
    except Exception:
        pass
    await _finalize_message(callback.message, session, bot, state)


@router.callback_query(F.data == "likes:view")
async def cb_view_likes(
    callback: CallbackQuery, session: AsyncSession, bot: Bot
) -> None:
    user = await load_user(session, callback.from_user.id)
    assert user and callback.message
    rows = await list_unseen_likers(session, user.tg_id)
    text = format_likes_list(rows, user.language)
    likes = [like for _, _, like in rows]
    await mark_likes_seen(session, user.tg_id)
    await callback.answer()
    try:
        await callback.message.edit_reply_markup(reply_markup=None)
    except Exception:
        pass
    await callback.message.answer(
        text,
        parse_mode="Markdown",
        disable_web_page_preview=True,
        reply_markup=main_menu_kb(user.language),
    )
    for like in likes:
        await deliver_like_media(bot, user.tg_id, like)
