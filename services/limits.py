from __future__ import annotations

from datetime import UTC, date, datetime

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from database.models import DailyLikeStat, Gender, Profile, User
from services.settings_service import get_daily_like_limit
from services.users import is_premium


def utc_today() -> date:
    return datetime.now(UTC).date()


async def get_like_count_today(session: AsyncSession, user_id: int) -> int:
    result = await session.execute(
        select(DailyLikeStat).where(
            DailyLikeStat.user_id == user_id,
            DailyLikeStat.utc_date == utc_today(),
        )
    )
    row = result.scalar_one_or_none()
    return row.count if row else 0


async def consume_like_slot(
    session: AsyncSession,
    user: User,
    profile: Profile | None,
) -> bool:
    """Atomically reserve one daily like slot in the caller's transaction."""
    if not await is_like_limited(session, user, profile):
        return True

    limit = await get_daily_like_limit(session)
    if limit <= 0:
        return False

    day = utc_today()
    stmt = (
        insert(DailyLikeStat)
        .values(user_id=user.tg_id, utc_date=day, count=1)
        .on_conflict_do_update(
            constraint="uq_daily_like",
            set_={"count": DailyLikeStat.count + 1},
            where=DailyLikeStat.count < limit,
        )
        .returning(DailyLikeStat.count)
    )
    result = await session.execute(stmt)
    return result.scalar_one_or_none() is not None


async def is_like_limited(session: AsyncSession, user: User, profile: Profile | None) -> bool:
    """True if user is subject to daily like/browse limit."""
    if is_premium(user):
        return False
    if profile and profile.gender == Gender.female:
        return False
    return True


async def can_browse(session: AsyncSession, user: User, profile: Profile | None) -> bool:
    if not await is_like_limited(session, user, profile):
        return True
    limit = await get_daily_like_limit(session)
    used = await get_like_count_today(session, user.tg_id)
    return used < limit


async def can_like(session: AsyncSession, user: User, profile: Profile | None) -> bool:
    return await can_browse(session, user, profile)
