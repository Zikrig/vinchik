from __future__ import annotations

from datetime import UTC, datetime, timedelta

from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from database.models import Profile, Report, User

REPORT_WINDOW_DAYS = 90
REPORT_BLOCK_THRESHOLD = 5  # more than 5 → block
REPORT_SUSPICIOUS_THRESHOLD = 2  # at least 2 → suspicious


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

    target_exists = await session.get(User, to_user_id)
    if target_exists is None:
        return False, False

    created_id = (
        await session.execute(
            insert(Report)
            .values(from_user_id=from_user_id, to_user_id=to_user_id)
            .on_conflict_do_nothing(constraint="uq_report_pair")
            .returning(Report.id)
        )
    ).scalar_one_or_none()
    if created_id is None:
        # ON CONFLICT DO NOTHING leaves the transaction usable; a rollback here
        # would expire the caller's User/Profile and blow up on next attribute.
        return False, False

    target = (
        await session.execute(
            select(User)
            .where(User.tg_id == to_user_id)
            .options(selectinload(User.profile))
            .with_for_update()
            .execution_options(populate_existing=True)
        )
    ).scalar_one()
    count = await count_reports_recent(session, to_user_id)
    if count >= REPORT_SUSPICIOUS_THRESHOLD:
        from services.moderation import mark_suspicious

        await mark_suspicious(
            session,
            to_user_id,
            f"жалоб за {REPORT_WINDOW_DAYS} дн.: {count}",
            commit=False,
        )
    just_blocked = False
    if count > REPORT_BLOCK_THRESHOLD and not target.is_blocked:
        target.is_blocked = True
        target.blocked_at = datetime.now(UTC)
        if target.profile:
            target.profile.is_active = False
        just_blocked = True
    await session.commit()
    return True, just_blocked


async def list_blocked_users(session: AsyncSession) -> list[dict]:
    """Plain dicts for templates — no ORM lazy access after session closes."""
    result = await session.execute(
        select(User, Profile)
        .outerjoin(Profile, Profile.user_id == User.tg_id)
        .where(User.is_blocked.is_(True))
        .order_by(User.blocked_at.desc().nulls_last())
    )
    users = result.all()
    user_ids = [user.tg_id for user, _ in users]
    report_counts: dict[int, int] = {}
    if user_ids:
        since = datetime.now(UTC) - timedelta(days=REPORT_WINDOW_DAYS)
        count_result = await session.execute(
            select(Report.to_user_id, func.count())
            .where(Report.to_user_id.in_(user_ids), Report.created_at >= since)
            .group_by(Report.to_user_id)
        )
        report_counts = {int(uid): int(n) for uid, n in count_result.all()}

    out: list[dict] = []
    for user, profile in users:
        out.append(
            {
                "tg_id": user.tg_id,
                "username": user.username,
                "blocked_at": user.blocked_at,
                "reports_n": report_counts.get(user.tg_id, 0),
                "profile": None
                if profile is None
                else {
                    "photo_file_id": profile.photo_file_id,
                    "name": profile.name,
                    "age": profile.age,
                    "city_name": profile.city_name,
                    "gender": profile.gender.value if profile.gender else None,
                    "looking_for": profile.looking_for.value if profile.looking_for else None,
                    "lat": profile.lat,
                    "lon": profile.lon,
                    "description": profile.description,
                },
            }
        )
    return out


async def unban_user(session: AsyncSession, user_id: int) -> User | None:
    from services.users import load_user_with_profile

    user = await load_user_with_profile(session, user_id)
    if user is None:
        return None
    user.is_blocked = False
    user.blocked_at = None
    if user.profile and user.profile.is_complete:
        user.profile.is_active = True
    await session.commit()
    return user


async def ban_user(session: AsyncSession, user_id: int) -> User | None:
    from services.users import load_user_with_profile

    user = await load_user_with_profile(session, user_id)
    if user is None:
        return None
    if not user.is_blocked:
        user.is_blocked = True
        user.blocked_at = datetime.now(UTC)
        if user.profile:
            user.profile.is_active = False
        await session.commit()
    return user
