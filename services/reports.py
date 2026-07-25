from __future__ import annotations

from datetime import UTC, datetime, timedelta

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from database.models import Profile, Report, User

REPORT_WINDOW_DAYS = 90
REPORT_BLOCK_THRESHOLD = 5  # more than 5 → block


async def count_reports_recent(session: AsyncSession, to_user_id: int) -> int:
    since = datetime.now(UTC) - timedelta(days=REPORT_WINDOW_DAYS)
    result = await session.execute(
        select(func.count())
        .select_from(Report)
        .where(Report.to_user_id == to_user_id, Report.created_at >= since)
    )
    return int(result.scalar_one())


async def file_report(
    session: AsyncSession,
    from_user_id: int,
    to_user_id: int,
) -> tuple[bool, bool]:
    """
    Returns (created_new_report, just_blocked).
    Duplicate reports from the same user are ignored.
    """
    if from_user_id == to_user_id:
        return False, False

    existing = await session.execute(
        select(Report).where(
            Report.from_user_id == from_user_id,
            Report.to_user_id == to_user_id,
        )
    )
    if existing.scalar_one_or_none():
        return False, False

    session.add(Report(from_user_id=from_user_id, to_user_id=to_user_id))
    await session.commit()

    count = await count_reports_recent(session, to_user_id)
    just_blocked = False
    if count > REPORT_BLOCK_THRESHOLD:
        target = await session.get(User, to_user_id, options=[selectinload(User.profile)])
        if target and not target.is_blocked:
            target.is_blocked = True
            target.blocked_at = datetime.now(UTC)
            if target.profile:
                target.profile.is_active = False
            await session.commit()
            just_blocked = True
    return True, just_blocked


async def list_blocked_users(session: AsyncSession) -> list[tuple[User, Profile | None, int]]:
    result = await session.execute(
        select(User)
        .where(User.is_blocked.is_(True))
        .options(selectinload(User.profile))
        .order_by(User.blocked_at.desc().nulls_last())
    )
    users = list(result.scalars().all())
    out: list[tuple[User, Profile | None, int]] = []
    for u in users:
        n = await count_reports_recent(session, u.tg_id)
        out.append((u, u.profile, n))
    return out


async def unban_user(session: AsyncSession, user_id: int) -> User | None:
    user = await session.get(User, user_id, options=[selectinload(User.profile)])
    if user is None:
        return None
    user.is_blocked = False
    user.blocked_at = None
    if user.profile and user.profile.is_complete:
        user.profile.is_active = True
    await session.commit()
    return user
