from __future__ import annotations

from datetime import UTC, datetime, timedelta

from aiogram import Bot
from aiogram.exceptions import TelegramAPIError
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from config import settings
from database.models import Like, LikeAction, Profile, User
from locales import t
from services.limits import can_like, increment_like_count


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


async def mark_likes_seen(session: AsyncSession, user_id: int) -> None:
    result = await session.execute(
        select(Like).where(
            Like.to_user_id == user_id,
            Like.action.in_([LikeAction.like, LikeAction.message]),
            Like.is_seen.is_(False),
        )
    )
    for like in result.scalars().all():
        like.is_seen = True
    await session.commit()


async def record_action(
    session: AsyncSession,
    from_user: User,
    from_profile: Profile,
    to_user_id: int,
    action: LikeAction,
    message_text: str | None = None,
) -> Like | None:
    """Save like/dislike/message. Sleep must never call this.

    One row per pair (unique). Already rated pairs stay hidden from the feed forever;
    a second reaction on the same pair is ignored.
    """
    existing = (
        await session.execute(
            select(Like).where(
                Like.from_user_id == from_user.tg_id, Like.to_user_id == to_user_id
            )
        )
    ).scalar_one_or_none()

    if existing is not None:
        return None

    if action in (LikeAction.like, LikeAction.message):
        if not await can_like(session, from_user, from_profile):
            raise PermissionError("limit")
        await increment_like_count(session, from_user.tg_id)

    like = Like(
        from_user_id=from_user.tg_id,
        to_user_id=to_user_id,
        action=action,
        message_text=message_text,
        created_at=datetime.now(UTC),
    )
    session.add(like)
    await session.commit()
    return like


async def notify_like_batch(bot: Bot, session: AsyncSession, to_user_id: int) -> None:
    target = await session.get(User, to_user_id)
    if target is None or target.is_test or to_user_id <= 0:
        return

    n = await unseen_likes_count(session, to_user_id)
    if n <= 0:
        return

    now = datetime.now(UTC)
    last = target.last_like_notify_at
    if last is not None:
        if last.tzinfo is None:
            last = last.replace(tzinfo=UTC)
        if now - last < timedelta(minutes=settings.like_notify_interval_minutes):
            return

    lang = target.language or "ru"
    text = t("liked_one", lang) if n == 1 else t("liked_many", lang, n=n)
    kb = InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text=t("btn_view_likes", lang), callback_data="likes:view")]
        ]
    )

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


def format_likes_list(rows: list[tuple[User, Profile, Like]], lang: str) -> str:
    if not rows:
        return t("likes_list_empty", lang)
    lines = [t("likes_list_title", lang)]
    for user, profile, like in rows:
        name = profile.name or "—"
        if user.username:
            # Escape for Markdown links
            safe_name = name.replace("[", "\\[").replace("]", "\\]")
            lines.append(f"[{safe_name}](https://t.me/{user.username})")
        else:
            lines.append(name)
        if like.message_text:
            text = like.message_text.replace('"', "'")
            lines.append(t("likes_list_message", lang, text=text))
        if len(rows) > 1:
            lines.append("")
    return "\n".join(lines).rstrip()
