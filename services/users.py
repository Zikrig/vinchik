from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from database.models import Profile, User


async def get_or_create_user(
    session: AsyncSession,
    tg_id: int,
    username: str | None,
    language: str | None = None,
) -> User:
    user = await session.get(User, tg_id, options=[selectinload(User.profile)])
    if user is None:
        user = User(tg_id=tg_id, username=username, language=language or "ru")
        session.add(user)
        session.add(Profile(user_id=tg_id))
        await session.commit()
        user = await session.get(User, tg_id, options=[selectinload(User.profile)])
        assert user is not None
        return user
    if username and user.username != username:
        user.username = username
        await session.commit()
    return user


async def set_language(session: AsyncSession, user: User, lang: str) -> None:
    user.language = lang
    await session.commit()


def is_premium(user: User) -> bool:
    if user.premium_until is None:
        return False
    now = datetime.now(UTC)
    until = user.premium_until
    if until.tzinfo is None:
        until = until.replace(tzinfo=UTC)
    return until > now
