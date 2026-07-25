from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy.ext.asyncio import AsyncSession

from database.models import User


async def touch_activity(session: AsyncSession, user: User) -> None:
    user.last_activity_at = datetime.now(UTC)
    user.reengage_level = 0
    await session.commit()
