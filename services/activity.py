from __future__ import annotations

from datetime import UTC, datetime, timedelta

from sqlalchemy.ext.asyncio import AsyncSession

from database.models import User
from services.performance import timed

ACTIVITY_WRITE_INTERVAL = timedelta(minutes=5)


def mark_activity_if_stale(user: User, now: datetime | None = None) -> bool:
    """Update ORM fields at most once per interval; return whether they changed."""
    current = now or datetime.now(UTC)
    previous = user.last_activity_at
    if previous is not None:
        if previous.tzinfo is None:
            previous = previous.replace(tzinfo=UTC)
        if (
            current - previous < ACTIVITY_WRITE_INTERVAL
            and user.reengage_level == 0
        ):
            return False
    user.last_activity_at = current
    user.reengage_level = 0
    return True


@timed("activity.touch")
async def touch_activity(session: AsyncSession, user: User) -> None:
    if mark_activity_if_stale(user):
        await session.commit()
