from __future__ import annotations

import asyncio
import html
import json
import logging
from datetime import UTC, datetime, timedelta
from typing import Any

from aiogram import Bot
from aiogram.exceptions import TelegramAPIError
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup
from sqlalchemy import and_, func, select, update
from sqlalchemy.orm import selectinload
from sqlalchemy.ext.asyncio import AsyncSession

from config import settings
from database.models import Like, LikeAction, Profile, User
from database.session import async_session_maker
from locales import t
from services.activity import mark_activity_if_stale
from services.limits import consume_like_slot
from services.performance import timed

MAX_MESSAGE_ATTACHMENTS = 3  # unused; kept for imports safety
MAX_MESSAGE_ATTACH_BYTES = 10 * 1024 * 1024
logger = logging.getLogger(__name__)
_notification_tasks: set[asyncio.Task[None]] = set()


def empty_message_payload() -> dict[str, Any]:
    return {
        "text": None,
        "voice_file_id": None,
        "video_note_file_id": None,
    }


def payload_text(payload: dict | None) -> str | None:
    if not payload:
        return None
    text = (payload.get("text") or "").strip()
    return text or None


async def unseen_likes_count(session: AsyncSession, user_id: int) -> int:
    result = await session.execute(
        select(func.count())
        .select_from(Like)
        .where(
            Like.to_user_id == user_id,
            Like.action.in_([LikeAction.like, LikeAction.message]),
            Like.is_seen.is_(False),
        )
    )
    return int(result.scalar_one())


async def list_unseen_likers(session: AsyncSession, user_id: int) -> list[tuple[User, Profile, Like]]:
    result = await session.execute(
        select(Like, User, Profile)
        .join(User, User.tg_id == Like.from_user_id)
        .join(Profile, Profile.user_id == Like.from_user_id)
        .where(
            Like.to_user_id == user_id,
            Like.action.in_([LikeAction.like, LikeAction.message]),
            Like.is_seen.is_(False),
        )
        .order_by(Like.created_at.desc())
    )
    return [(u, p, like) for like, u, p in result.all()]


async def next_unseen_liker(
    session: AsyncSession, user_id: int
) -> tuple[User, Profile, Like] | None:
    """Oldest unseen incoming like with a profile (inbox card queue)."""
    result = await session.execute(
        select(Like, User, Profile)
        .join(User, User.tg_id == Like.from_user_id)
        .join(Profile, Profile.user_id == Like.from_user_id)
        .where(
            Like.to_user_id == user_id,
            Like.action.in_([LikeAction.like, LikeAction.message]),
            Like.is_seen.is_(False),
            User.is_blocked.is_(False),
        )
        .order_by(Like.created_at.asc())
        .limit(1)
    )
    row = result.one_or_none()
    if row is None:
        return None
    like, user, profile = row
    return user, profile, like


async def mark_like_seen(session: AsyncSession, like_id: int) -> None:
    await session.execute(
        update(Like).where(Like.id == like_id).values(is_seen=True)
    )
    await session.commit()


async def mark_likes_seen(session: AsyncSession, user_id: int) -> None:
    await session.execute(
        update(Like)
        .where(
            Like.to_user_id == user_id,
            Like.action.in_([LikeAction.like, LikeAction.message]),
            Like.is_seen.is_(False),
        )
        .values(is_seen=True)
    )
    await session.commit()


@timed("likes.record_action")
async def record_action(
    session: AsyncSession,
    from_user: User,
    from_profile: Profile,
    to_user_id: int,
    action: LikeAction,
    message_text: str | None = None,
    message_payload: dict | None = None,
) -> Like | None:
    """Save like/dislike/message. Sleep must never call this.

    One row per directed pair (unique). Hides both users from each other for
    ``profile_reshow_days`` (0 = forever). After that window a new reaction
    updates the same row.

    Returns None if the target no longer exists (stale browse card after
    test-user wipe) or the pair is still hidden by the reshow window.
    """
    from services.settings_service import get_profile_reshow_days

    # Serialize all reactions from one sender. This closes duplicate-pair and
    # daily-limit races while keeping counter + reaction in one transaction.
    locked_user = (
        await session.execute(
            select(User)
            .where(User.tg_id == from_user.tg_id)
            .options(selectinload(User.profile))
            .with_for_update()
            .execution_options(populate_existing=True)
        )
    ).scalar_one()
    from_user = locked_user
    if from_user.profile is not None:
        from_profile = from_user.profile

    if from_user.is_blocked:
        raise PermissionError("blocked")

    target_row = (
        await session.execute(
            select(User, Like)
            .outerjoin(
                Like,
                and_(
                    Like.from_user_id == from_user.tg_id,
                    Like.to_user_id == to_user_id,
                ),
            )
            .where(User.tg_id == to_user_id)
        )
    ).one_or_none()
    if target_row is None:
        await session.rollback()
        return None
    _target, existing = target_row

    if existing is not None:
        reshow_days = await get_profile_reshow_days(session)
        if reshow_days <= 0:
            await session.rollback()
            return None
        created = existing.created_at
        if created.tzinfo is None:
            created = created.replace(tzinfo=UTC)
        if datetime.now(UTC) - created < timedelta(days=reshow_days):
            await session.rollback()
            return None

    if action in (LikeAction.like, LikeAction.message):
        if not await consume_like_slot(session, from_user, from_profile):
            raise PermissionError("limit")

    text = message_text
    payload = message_payload
    if payload is not None:
        text = payload_text(payload) or text

    now = datetime.now(UTC)
    mark_activity_if_stale(from_user, now)
    if existing is not None:
        existing.action = action
        existing.message_text = text
        existing.message_payload = payload
        existing.is_seen = False
        existing.created_at = now
        if action in (LikeAction.like, LikeAction.message):
            from services.moderation import on_like_recorded

            await session.flush()
            await on_like_recorded(
                session, from_user.tg_id, action, text, commit=False
            )
        await session.commit()
        return existing

    like = Like(
        from_user_id=from_user.tg_id,
        to_user_id=to_user_id,
        action=action,
        message_text=text,
        message_payload=payload,
        created_at=now,
    )
    session.add(like)
    if action in (LikeAction.like, LikeAction.message):
        from services.moderation import on_like_recorded

        await session.flush()
        await on_like_recorded(session, from_user.tg_id, action, text, commit=False)
    await session.commit()
    return like


async def notify_like_batch(bot: Bot, session: AsyncSession, to_user_id: int) -> None:
    target = await session.get(User, to_user_id)
    if target is None or target.is_test or to_user_id <= 0:
        await session.commit()
        return

    n = await unseen_likes_count(session, to_user_id)
    if n <= 0:
        await session.commit()
        return

    now = datetime.now(UTC)
    last = target.last_like_notify_at
    within_window = False
    if last is not None:
        if last.tzinfo is None:
            last = last.replace(tzinfo=UTC)
        within_window = now - last < timedelta(minutes=settings.like_notify_interval_minutes)

    lang = target.language or "ru"
    text = t("liked_one", lang) if n == 1 else t("liked_many", lang, n=n)
    kb = InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text=t("btn_view_likes", lang), callback_data="likes:view")]
        ]
    )
    # Do not hold a DB connection while Telegram sends/edits the notification.
    await session.commit()

    # Within the batch window: refresh the same notification instead of staying silent.
    if within_window and target.likes_notify_message_id:
        try:
            await bot.edit_message_text(
                text,
                chat_id=to_user_id,
                message_id=target.likes_notify_message_id,
                reply_markup=kb,
            )
            return
        except TelegramAPIError:
            pass

    if target.likes_notify_message_id:
        try:
            await bot.edit_message_reply_markup(
                chat_id=to_user_id,
                message_id=target.likes_notify_message_id,
                reply_markup=None,
            )
        except TelegramAPIError:
            pass

    try:
        msg = await bot.send_message(to_user_id, text, reply_markup=kb)
    except TelegramAPIError:
        # Blocked bot / deleted chat / never started — like is already saved.
        return

    target.likes_notify_message_id = msg.message_id
    target.last_like_notify_at = now
    await session.commit()


def schedule_like_notification(bot: Bot, to_user_id: int) -> None:
    """Notify recipient out of sender's latency-critical swipe path."""

    async def run() -> None:
        async with async_session_maker() as session:
            await notify_like_batch(bot, session, to_user_id)

    task = asyncio.create_task(run(), name=f"like-notify-{to_user_id}")
    _notification_tasks.add(task)
    task.add_done_callback(_notification_tasks.discard)

    def log_failure(done: asyncio.Task[None]) -> None:
        if done.cancelled():
            return
        if exc := done.exception():
            logger.warning("like notification failed for %s: %r", to_user_id, exc)

    task.add_done_callback(log_failure)


def _like_payload(like: Like) -> dict:
    raw = like.message_payload
    if isinstance(raw, dict):
        return raw
    if isinstance(raw, str):
        try:
            data = json.loads(raw)
            return data if isinstance(data, dict) else {}
        except json.JSONDecodeError:
            return {}
    return {}


def format_likes_list(rows: list[tuple[User, Profile, Like]], lang: str) -> str:
    if not rows:
        return html.escape(t("likes_list_empty", lang))
    lines = [html.escape(t("likes_list_title", lang))]
    for user, profile, like in rows:
        name = html.escape(profile.name or "—")
        if user.username:
            lines.append(f'<a href="https://t.me/{user.username}">{name}</a>')
        else:
            lines.append(name)
        payload = _like_payload(like)
        text = payload_text(payload) or (like.message_text or "").strip()
        if text:
            lines.append(
                t(
                    "likes_list_message",
                    lang,
                    text=html.escape(text.replace('"', "'")),
                )
            )
        if payload.get("voice_file_id"):
            lines.append(html.escape(t("likes_list_voice", lang)))
        if payload.get("video_note_file_id"):
            lines.append(html.escape(t("likes_list_video_note", lang)))
        if len(rows) > 1:
            lines.append("")
    return "\n".join(lines).rstrip()


async def deliver_like_media(bot: Bot, chat_id: int, like: Like) -> bool:
    """Forward stored voice / video note to the recipient. Returns True if all sent."""
    payload = _like_payload(like)
    ok = True
    try:
        if payload.get("voice_file_id"):
            await bot.send_voice(chat_id, payload["voice_file_id"])
        if payload.get("video_note_file_id"):
            await bot.send_video_note(chat_id, payload["video_note_file_id"])
    except TelegramAPIError:
        ok = False
    return ok
