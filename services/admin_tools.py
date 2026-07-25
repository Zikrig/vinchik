from __future__ import annotations

import random
from datetime import UTC, datetime

from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from database.models import Gender, LookingFor, Profile, User
from services.media import TEST_PHOTO_MARKER
from services.users import get_or_create_user

# Dushanbe, capital of Tajikistan
DUSHANBE_LAT = 38.5598
DUSHANBE_LON = 68.7870
DUSHANBE_CITY = "Душанбе"

# Tajikistan + nearby border belt
TJ_LAT_MIN, TJ_LAT_MAX = 36.0, 41.8
TJ_LON_MIN, TJ_LON_MAX = 66.5, 76.0

_TEST_NAMES = (
    "Али",
    "Фарход",
    "Дилшод",
    "Рустам",
    "Зарина",
    "Нигора",
    "Мадина",
    "Шахло",
    "Бахтиёр",
    "Парвина",
    "Ситора",
    "Джамшед",
)


async def set_user_geo(
    session: AsyncSession,
    tg_id: int,
    lat: float,
    lon: float,
    city_name: str = DUSHANBE_CITY,
) -> User:
    user = await get_or_create_user(session, tg_id, username=None)
    assert user.profile is not None
    user.profile.lat = lat
    user.profile.lon = lon
    user.profile.city_name = city_name.strip() or DUSHANBE_CITY
    await session.commit()
    return user


def _random_tj_coords() -> tuple[float, float]:
    return (
        round(random.uniform(TJ_LAT_MIN, TJ_LAT_MAX), 6),
        round(random.uniform(TJ_LON_MIN, TJ_LON_MAX), 6),
    )


async def _next_test_tg_id(session: AsyncSession) -> int:
    """Negative ids reserved for test accounts (never collide with Telegram)."""
    result = await session.execute(
        select(func.min(User.tg_id)).where(User.is_test.is_(True))
    )
    current_min = result.scalar_one_or_none()
    if current_min is None or current_min >= 0:
        return -1
    return int(current_min) - 1


async def create_test_users(
    session: AsyncSession,
    count: int,
    *,
    visible: bool = True,
) -> int:
    count = max(0, min(int(count), 100))
    created = 0
    for _ in range(count):
        tg_id = await _next_test_tg_id(session)
        lat, lon = _random_tj_coords()
        gender = random.choice((Gender.male, Gender.female))
        if gender == Gender.male:
            looking = random.choice((LookingFor.female, LookingFor.any))
        else:
            looking = random.choice((LookingFor.male, LookingFor.any))
        name = random.choice(_TEST_NAMES)
        age = random.randint(18, 35)
        user = User(
            tg_id=tg_id,
            username=f"test_{abs(tg_id)}",
            language="tg",
            is_test=True,
            last_activity_at=datetime.now(UTC),
        )
        profile = Profile(
            user_id=tg_id,
            name=name,
            age=age,
            gender=gender,
            looking_for=looking,
            lat=lat,
            lon=lon,
            city_name="Тоҷикистон",
            description="Тестовая анкета",
            photo_file_id=TEST_PHOTO_MARKER,
            is_active=visible,
            is_complete=True,
        )
        session.add(user)
        session.add(profile)
        await session.flush()
        created += 1
    await session.commit()
    return created


async def count_test_users(session: AsyncSession) -> int:
    result = await session.execute(
        select(func.count()).select_from(User).where(User.is_test.is_(True))
    )
    return int(result.scalar_one())


async def clear_test_users(session: AsyncSession) -> int:
    result = await session.execute(select(User.tg_id).where(User.is_test.is_(True)))
    ids = list(result.scalars().all())
    if not ids:
        return 0
    # profiles cascade from users; likes/reports may reference — delete dependents first
    from database.models import DailyLikeStat, Like, PremiumOrder, Report

    await session.execute(delete(Like).where(Like.from_user_id.in_(ids)))
    await session.execute(delete(Like).where(Like.to_user_id.in_(ids)))
    await session.execute(delete(Report).where(Report.from_user_id.in_(ids)))
    await session.execute(delete(Report).where(Report.to_user_id.in_(ids)))
    await session.execute(delete(DailyLikeStat).where(DailyLikeStat.user_id.in_(ids)))
    await session.execute(delete(PremiumOrder).where(PremiumOrder.user_id.in_(ids)))
    await session.execute(delete(Profile).where(Profile.user_id.in_(ids)))
    await session.execute(delete(User).where(User.tg_id.in_(ids)))
    await session.commit()
    return len(ids)


async def get_user_geo(session: AsyncSession, tg_id: int) -> dict[str, float | str | None]:
    user = await session.get(User, tg_id, options=[selectinload(User.profile)])
    if not user or not user.profile:
        return {"lat": None, "lon": None, "city_name": None}
    return {
        "lat": user.profile.lat,
        "lon": user.profile.lon,
        "city_name": user.profile.city_name,
    }
