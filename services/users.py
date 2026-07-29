from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy import select
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
    user = await load_user_with_profile(session, tg_id)
    if user is None:
        user = User(tg_id=tg_id, username=username, language=language or "ru")
        session.add(user)
        session.add(Profile(user_id=tg_id))
        await session.commit()
        user = await load_user_with_profile(session, tg_id)
        assert user is not None
        return user
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


def is_premium(user: User) -> bool:
    if user.premium_until is None:
        return False
    now = datetime.now(UTC)
    until = user.premium_until
    if until.tzinfo is None:
        until = until.replace(tzinfo=UTC)
    return until > now
