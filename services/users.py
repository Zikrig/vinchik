from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from database.models import Profile, User


async def load_user_with_profile(session: AsyncSession, tg_id: int) -> User | None:
    """Always query+selectinload — session.get(options=...) can skip reload from identity map."""
    result = await session.execute(
        select(User).where(User.tg_id == tg_id).options(selectinload(User.profile))
    )
    return result.scalar_one_or_none()


async def get_or_create_user(
    session: AsyncSession,
    tg_id: int,
    username: str | None,
    language: str | None = None,
) -> User:
    await session.execute(
        insert(User)
        .values(tg_id=tg_id, username=username, language=language or "ru")
        .on_conflict_do_nothing(index_elements=[User.tg_id])
    )
    await session.execute(
        insert(Profile)
        .values(user_id=tg_id)
        .on_conflict_do_nothing(index_elements=[Profile.user_id])
    )
    await session.commit()

    user = await load_user_with_profile(session, tg_id)
    assert user is not None
    if username and user.username != username:
        user.username = username
        await session.commit()
        user = await load_user_with_profile(session, tg_id)
        assert user is not None
    return user


async def set_language(session: AsyncSession, user: User, lang: str) -> None:
    user.language = lang
    user.language_chosen = True
    await session.commit()


def aware_utc(dt: datetime | None) -> datetime | None:
    if dt is None:
        return None
    if dt.tzinfo is None:
        return dt.replace(tzinfo=UTC)
    return dt


def premium_extension_base(user: User, now: datetime | None = None) -> datetime:
    """Start of premium extension: active until or now."""
    now = now or datetime.now(UTC)
    until = aware_utc(user.premium_until)
    if until is not None and until > now:
        return until
    return now


def is_premium(user: User) -> bool:
    if user.premium_until is None:
        return False
    until = aware_utc(user.premium_until)
    assert until is not None
    return until > datetime.now(UTC)
