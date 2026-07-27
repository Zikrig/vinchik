from __future__ import annotations

import math
import random
from datetime import UTC, datetime

from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from config import settings
from database.models import Gender, LookingFor, Profile, User
from services.media import TEST_PHOTO_MARKER
from services.users import get_or_create_user

# Dushanbe, capital of Tajikistan
DUSHANBE_LAT = 38.5598
DUSHANBE_LON = 68.7870
DUSHANBE_CITY = "Душанбе"

# Spawn test profiles near center (not across whole TJ).
TEST_SPAWN_RADIUS_KM = 15.0

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


def _random_near(
    lat: float, lon: float, radius_km: float = TEST_SPAWN_RADIUS_KM
) -> tuple[float, float]:
    """Uniform random point in a circle around (lat, lon)."""
    r = radius_km * math.sqrt(random.random())
    theta = random.uniform(0, 2 * math.pi)
    dlat = (r * math.cos(theta)) / 111.0
    cos_lat = math.cos(math.radians(lat)) or 1e-6
    dlon = (r * math.sin(theta)) / (111.0 * cos_lat)
    return round(lat + dlat, 6), round(lon + dlon, 6)


async def _test_spawn_center(session: AsyncSession) -> tuple[float, float, str]:
    """Prefer first admin with saved geo, else Dushanbe."""
    for aid in sorted(settings.admin_id_set):
        geo = await get_user_geo(session, aid)
        if geo["lat"] is not None and geo["lon"] is not None:
            return (
                float(geo["lat"]),
                float(geo["lon"]),
                str(geo["city_name"] or DUSHANBE_CITY),
            )
    return DUSHANBE_LAT, DUSHANBE_LON, DUSHANBE_CITY


async def _next_test_tg_id(session: AsyncSession) -> int:
    """Negative ids reserved for test accounts (never collide with Telegram)."""
    result = await session.execute(
        select(func.min(User.tg_id)).where(User.is_test.is_(True))
    )
    current_min = result.scalar_one_or_none()
    if current_min is None or current_min >= 0:
        return -1
    return int(current_min) - 1


async def create_test_users(session: AsyncSession, count: int) -> int:
    count = max(0, min(int(count), 100))
    visible = await are_test_users_visible(session)
    center_lat, center_lon, city = await _test_spawn_center(session)
    created = 0
    for _ in range(count):
        tg_id = await _next_test_tg_id(session)
        lat, lon = _random_near(center_lat, center_lon)
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
            city_name=city,
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


async def are_test_users_visible(session: AsyncSession) -> bool:
    """True if there are no test users yet, or all test profiles are active in feed."""
    total = await count_test_users(session)
    if total == 0:
        return True
    result = await session.execute(
        select(func.count())
        .select_from(Profile)
        .join(User, User.tg_id == Profile.user_id)
        .where(User.is_test.is_(True), Profile.is_active.is_(True))
    )
    return int(result.scalar_one()) == total


async def set_test_users_visible(session: AsyncSession, visible: bool) -> int:
    result = await session.execute(
        select(Profile)
        .join(User, User.tg_id == Profile.user_id)
        .where(User.is_test.is_(True))
    )
    profiles = list(result.scalars().all())
    for p in profiles:
        p.is_active = visible
    await session.commit()
    return len(profiles)

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
