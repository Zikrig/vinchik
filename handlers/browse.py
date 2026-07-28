from __future__ import annotations

from aiogram import Bot, F, Router
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message
from sqlalchemy.ext.asyncio import AsyncSession

from database.models import LikeAction
from handlers.common import load_user, show_main_menu
from keyboards.inline import browse_kb, channels_kb, main_menu_kb
from locales import t
from services.activity import touch_activity
from services.browse import next_profile, profile_caption
from services.channels import list_active_channels, user_subscribed_all
from services.media import as_photo_input
from services.likes import (
    format_likes_list,
    list_unseen_likers,
    mark_likes_seen,
    notify_like_batch,
    record_action,
)
from services.limits import can_browse, can_like
from services.reports import file_report
from services.settings_service import is_registration_only
from states.profile import ProfileStates

router = Router()


async def strip_card_keyboard(message: Message | None) -> None:
    """Remove inline buttons from the shown profile card after a reaction."""
    if message is None:
        return
    try:
        await message.edit_reply_markup(reply_markup=None)
    except Exception:
        pass


async def notify_limit(bot: Bot, callback: CallbackQuery, lang: str) -> None:
    """Always tell the user the daily limit is over — do not rely on callback.message."""
    try:
        await callback.answer()
    except Exception:
        pass
    await strip_card_keyboard(callback.message)
    await bot.send_message(
        callback.from_user.id,
        t("limit_reached", lang),
        reply_markup=main_menu_kb(lang),
    )


async def start_browse(
    message: Message, session: AsyncSession, user, bot: Bot | None = None
) -> None:
    lang = user.language or "ru"
    dest = bot or message.bot
    chat_id = user.tg_id

    async def say(text: str, **kwargs) -> None:
        await dest.send_message(chat_id, text, **kwargs)

    if user.is_blocked:
        await say(t("you_are_blocked", lang))
        return
    if await is_registration_only(session):
        await say(t("soft_launch", lang), reply_markup=main_menu_kb(lang))
        return
    if not await user_subscribed_all(dest, session, user):
        channels = await list_active_channels(session)
        await say(t("need_channels", lang), reply_markup=channels_kb(lang, channels))
        return
    if not await can_browse(session, user, user.profile):
        await say(t("limit_reached", lang), reply_markup=main_menu_kb(lang))
        return
    if not user.profile or not user.profile.is_complete:
        await say(t("ask_age", lang), reply_markup=main_menu_kb(lang))
        return
    if not user.profile.is_active:
        user.profile.is_active = True
        await session.commit()

    await touch_activity(session, user)

    profile = await next_profile(session, user, user.profile)
    if profile is None:
        await say(t("empty_feed", lang), reply_markup=main_menu_kb(lang))
        return
    caption = profile_caption(profile)
    kb = browse_kb(lang, profile.user_id)
    photo = as_photo_input(profile.photo_file_id)
    if photo is not None:
        await dest.send_photo(chat_id, photo, caption=caption, reply_markup=kb)
    else:
        await say(caption, reply_markup=kb)


@router.callback_query(F.data == "browse:start")
async def cb_browse(callback: CallbackQuery, session: AsyncSession, bot: Bot) -> None:
    user = await load_user(session, callback.from_user.id)
    assert user and callback.message
    await callback.answer()
    if not await can_browse(session, user, user.profile):
        await bot.send_message(
            callback.from_user.id,
            t("limit_reached", user.language or "ru"),
            reply_markup=main_menu_kb(user.language or "ru"),
        )
        return
    await start_browse(callback.message, session, user, bot)


@router.callback_query(F.data == "channels:check")
async def cb_channels(callback: CallbackQuery, session: AsyncSession, bot: Bot) -> None:
    user = await load_user(session, callback.from_user.id)
    assert user and callback.message
    if await user_subscribed_all(bot, session, user):
        await callback.answer(t("channels_ok", user.language))
        await start_browse(callback.message, session, user, bot)
    else:
        await callback.answer(t("channels_fail", user.language), show_alert=True)


@router.callback_query(F.data.startswith("b:like:"))
async def cb_like(callback: CallbackQuery, session: AsyncSession, bot: Bot) -> None:
    await _rate(callback, session, bot, LikeAction.like)


@router.callback_query(F.data.startswith("b:no:"))
async def cb_dislike(callback: CallbackQuery, session: AsyncSession, bot: Bot) -> None:
    await _rate(callback, session, bot, LikeAction.dislike)


@router.callback_query(F.data.startswith("b:msg:"))
async def cb_msg(
    callback: CallbackQuery, session: AsyncSession, state: FSMContext, bot: Bot
) -> None:
    user = await load_user(session, callback.from_user.id)
    assert user and callback.data and callback.message
    if not await can_browse(session, user, user.profile):
        await notify_limit(bot, callback, user.language or "ru")
        return
    target_id = int(callback.data.split(":")[2])
    await state.set_state(ProfileStates.send_message)
    await state.update_data(msg_target=target_id)
    await callback.answer()
    await strip_card_keyboard(callback.message)
    await bot.send_message(callback.from_user.id, t("ask_message", user.language or "ru"))


@router.message(ProfileStates.send_message)
async def send_message_text(
    message: Message, session: AsyncSession, state: FSMContext, bot: Bot
) -> None:
    user = await load_user(session, message.from_user.id)  # type: ignore[union-attr]
    assert user and user.profile
    data = await state.get_data()
    target_id = int(data["msg_target"])
    text = (message.text or "").strip()[:500]
    await state.clear()
    try:
        like = await record_action(
            session, user, user.profile, target_id, LikeAction.message, message_text=text
        )
    except PermissionError:
        await message.answer(
            t("limit_reached", user.language or "ru"),
            reply_markup=main_menu_kb(user.language or "ru"),
        )
        return
    if like:
        await notify_like_batch(bot, session, target_id)
        await message.answer(t("message_sent", user.language or "ru"))
    await start_browse(message, session, user, bot)


@router.callback_query(F.data == "b:sleep")
async def cb_sleep(callback: CallbackQuery, session: AsyncSession) -> None:
    """Pause browsing: strip buttons only. Does NOT record Like — card can reappear."""
    user = await load_user(session, callback.from_user.id)
    assert user and callback.message
    await callback.answer()
    await strip_card_keyboard(callback.message)
    await show_main_menu(callback.message, user)


@router.callback_query(F.data.startswith("b:rep:"))
async def cb_report(callback: CallbackQuery, session: AsyncSession, bot: Bot) -> None:
    user = await load_user(session, callback.from_user.id)
    assert user and user.profile and callback.data and callback.message
    if user.is_blocked:
        await callback.answer(t("you_are_blocked", user.language), show_alert=True)
        return
    if not await can_browse(session, user, user.profile):
        await notify_limit(bot, callback, user.language or "ru")
        return
    target_id = int(callback.data.split(":")[2])
    created, just_blocked = await file_report(session, user.tg_id, target_id)
    if created:
        await callback.answer(t("report_ok", user.language))
        try:
            await record_action(session, user, user.profile, target_id, LikeAction.dislike)
        except Exception:
            pass
        if just_blocked:
            target = await load_user(session, target_id)
            if target and not target.is_test:
                try:
                    await bot.send_message(
                        target_id, t("you_are_blocked", target.language or "ru")
                    )
                except Exception:
                    pass
    else:
        await callback.answer(t("report_dup", user.language), show_alert=True)
    await strip_card_keyboard(callback.message)
    await start_browse(callback.message, session, user, bot)


async def _rate(
    callback: CallbackQuery, session: AsyncSession, bot: Bot, action: LikeAction
) -> None:
    user = await load_user(session, callback.from_user.id)
    assert user and user.profile and callback.data and callback.message
    lang = user.language or "ru"
    target_id = int(callback.data.split(":")[2])

    # Block the whole feed after daily like/message limit (men without premium).
    if not await can_browse(session, user, user.profile):
        await notify_limit(bot, callback, lang)
        return

    try:
        like = await record_action(session, user, user.profile, target_id, action)
    except PermissionError:
        await notify_limit(bot, callback, lang)
        return

    await callback.answer()
    await strip_card_keyboard(callback.message)
    if like and action in (LikeAction.like, LikeAction.message):
        await notify_like_batch(bot, session, target_id)
    if like and action == LikeAction.like:
        await bot.send_message(callback.from_user.id, t("like_sent", lang))

    # If this like just exhausted the limit — stop with a clear message.
    if action in (LikeAction.like, LikeAction.message) and like is not None:
        if not await can_like(session, user, user.profile):
            await bot.send_message(
                callback.from_user.id,
                t("limit_reached", lang),
                reply_markup=main_menu_kb(lang),
            )
            return

    await start_browse(callback.message, session, user, bot)


@router.callback_query(F.data == "likes:view")
async def cb_view_likes(callback: CallbackQuery, session: AsyncSession) -> None:
    user = await load_user(session, callback.from_user.id)
    assert user and callback.message
    rows = await list_unseen_likers(session, user.tg_id)
    text = format_likes_list(rows, user.language)
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
