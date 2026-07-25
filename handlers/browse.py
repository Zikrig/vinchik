from __future__ import annotations

from aiogram import Bot, F, Router
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message
from sqlalchemy.ext.asyncio import AsyncSession

from database.models import LikeAction
from handlers.common import load_user, show_main_menu
from keyboards.inline import browse_kb, channels_kb, empty_feed_kb, main_menu_kb
from locales import t
from services.activity import touch_activity
from services.browse import next_profile, profile_caption
from services.channels import list_active_channels, user_subscribed_all
from services.likes import (
    format_likes_list,
    list_unseen_likers,
    mark_likes_seen,
    notify_like_batch,
    record_action,
)
from services.limits import can_browse
from services.reports import file_report
from services.settings_service import is_registration_only
from states.profile import ProfileStates

router = Router()


async def start_browse(message: Message, session: AsyncSession, user) -> None:
    lang = user.language
    if user.is_blocked:
        await message.answer(t("you_are_blocked", lang))
        return
    if await is_registration_only(session):
        await message.answer(t("soft_launch", lang))
        return
    if not await user_subscribed_all(message.bot, session, user):
        channels = await list_active_channels(session)
        await message.answer(
            t("need_channels", lang),
            reply_markup=channels_kb(lang, channels),
        )
        return
    if not await can_browse(session, user, user.profile):
        await message.answer(t("limit_reached", lang), reply_markup=main_menu_kb(lang))
        return
    if not user.profile or not user.profile.is_complete:
        await message.answer(t("ask_age", lang))
        return
    if not user.profile.is_active:
        user.profile.is_active = True
        await session.commit()

    await touch_activity(session, user)

    profile = await next_profile(session, user, user.profile)
    if profile is None:
        await message.answer(t("empty_feed", lang), reply_markup=empty_feed_kb(lang))
        return
    caption = profile_caption(profile)
    kb = browse_kb(lang, profile.user_id)
    if profile.photo_file_id:
        await message.answer_photo(profile.photo_file_id, caption=caption, reply_markup=kb)
    else:
        await message.answer(caption, reply_markup=kb)


@router.callback_query(F.data == "browse:start")
async def cb_browse(callback: CallbackQuery, session: AsyncSession) -> None:
    user = await load_user(session, callback.from_user.id)
    assert user and callback.message
    await callback.answer()
    await start_browse(callback.message, session, user)


@router.callback_query(F.data == "channels:check")
async def cb_channels(callback: CallbackQuery, session: AsyncSession) -> None:
    user = await load_user(session, callback.from_user.id)
    assert user and callback.message
    if await user_subscribed_all(callback.bot, session, user):
        await callback.answer(t("channels_ok", user.language))
        await start_browse(callback.message, session, user)
    else:
        await callback.answer(t("channels_fail", user.language), show_alert=True)


@router.callback_query(F.data.startswith("b:like:"))
async def cb_like(callback: CallbackQuery, session: AsyncSession, bot: Bot) -> None:
    await _rate(callback, session, bot, LikeAction.like)


@router.callback_query(F.data.startswith("b:no:"))
async def cb_dislike(callback: CallbackQuery, session: AsyncSession, bot: Bot) -> None:
    await _rate(callback, session, bot, LikeAction.dislike)


@router.callback_query(F.data.startswith("b:msg:"))
async def cb_msg(callback: CallbackQuery, session: AsyncSession, state: FSMContext) -> None:
    user = await load_user(session, callback.from_user.id)
    assert user and callback.data
    if not await can_browse(session, user, user.profile):
        await callback.answer(t("limit_reached", user.language), show_alert=True)
        return
    target_id = int(callback.data.split(":")[2])
    await state.set_state(ProfileStates.send_message)
    await state.update_data(msg_target=target_id)
    await callback.answer()
    assert callback.message
    await callback.message.answer(t("ask_message", user.language))


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
        await message.answer(t("limit_reached", user.language))
        return
    if like:
        await notify_like_batch(bot, session, target_id)
    await start_browse(message, session, user)


@router.callback_query(F.data == "b:sleep")
async def cb_sleep(callback: CallbackQuery, session: AsyncSession) -> None:
    user = await load_user(session, callback.from_user.id)
    assert user and callback.message
    await callback.answer()
    await show_main_menu(callback.message, user)


@router.callback_query(F.data.startswith("b:rep:"))
async def cb_report(callback: CallbackQuery, session: AsyncSession, bot: Bot) -> None:
    user = await load_user(session, callback.from_user.id)
    assert user and user.profile and callback.data and callback.message
    if user.is_blocked:
        await callback.answer(t("you_are_blocked", user.language), show_alert=True)
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
            if target:
                try:
                    await bot.send_message(
                        target_id, t("you_are_blocked", target.language or "ru")
                    )
                except Exception:
                    pass
    else:
        await callback.answer(t("report_dup", user.language), show_alert=True)
    await start_browse(callback.message, session, user)


async def _rate(
    callback: CallbackQuery, session: AsyncSession, bot: Bot, action: LikeAction
) -> None:
    user = await load_user(session, callback.from_user.id)
    assert user and user.profile and callback.data and callback.message
    target_id = int(callback.data.split(":")[2])
    try:
        like = await record_action(session, user, user.profile, target_id, action)
    except PermissionError:
        await callback.answer(t("limit_reached", user.language), show_alert=True)
        return
    await callback.answer()
    if like and action in (LikeAction.like, LikeAction.message):
        await notify_like_batch(bot, session, target_id)
    await start_browse(callback.message, session, user)


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
    await callback.message.answer(text, parse_mode="Markdown", disable_web_page_preview=True)
