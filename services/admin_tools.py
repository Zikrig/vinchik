from __future__ import annotations

import math
import random
from datetime import UTC, datetime

from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from config import settings
from database.models import Gender, LookingFor, Profile, User
from services.media import TEST_PHOTO_MARKER, local_photo_path
from services.users import get_or_create_user

# Dushanbe, capital of Tajikistan
DUSHANBE_LAT = 38.5598
DUSHANBE_LON = 68.7870
DUSHANBE_CITY = "Душанбе"

# Spawn test profiles near center (not across whole TJ).
TEST_SPAWN_RADIUS_KM = 60.0

_TEST_NAMES_MALE = (
    "Али",
    "Фарход",
    "Дилшод",
    "Рустам",
    "Бахтиёр",
    "Джамшед",
    "Камол",
    "Саид",
)
_TEST_NAMES_FEMALE = (
    "Зарина",
    "Нигора",
    "Мадина",
    "Шахло",
    "Парвина",
    "Ситора",
    "Мехри",
    "Гулнора",
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
    from services.settings_service import are_test_users_visible_setting

    visible = await are_test_users_visible_setting(session)
    center_lat, center_lon, city = await _test_spawn_center(session)
    test_photo = (
        TEST_PHOTO_MARKER
        if local_photo_path(TEST_PHOTO_MARKER) is not None
        else None
    )
    created = 0
    for _ in range(count):
        tg_id = await _next_test_tg_id(session)
        lat, lon = _random_near(center_lat, center_lon)
        gender = random.choice((Gender.male, Gender.female))
        if gender == Gender.male:
            looking = random.choice((LookingFor.female, LookingFor.any))
            name = random.choice(_TEST_NAMES_MALE)
        else:
            looking = random.choice((LookingFor.male, LookingFor.any))
            name = random.choice(_TEST_NAMES_FEMALE)
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
            photo_file_id=test_photo,
            photo_file_ids=[test_photo] if test_photo else None,
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
    """Admin switch state (setting). Profiles are synced when the switch flips."""
    from services.settings_service import are_test_users_visible_setting

    return await are_test_users_visible_setting(session)


async def set_test_users_visible(session: AsyncSession, visible: bool) -> int:
    from services.settings_service import set_setting

    result = await session.execute(
        select(Profile)
        .join(User, User.tg_id == Profile.user_id)
        .where(User.is_test.is_(True))
    )
    profiles = list(result.scalars().all())
    for p in profiles:
        p.is_active = visible
    await session.commit()
    await set_setting(session, "test_users_visible", "true" if visible else "false")
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
    """Read coordinates via columns only — never touch User.profile (no lazy IO)."""
    result = await session.execute(
        select(Profile.lat, Profile.lon, Profile.city_name).where(Profile.user_id == tg_id)
    )
    row = result.first()
    if row is None:
        return {"lat": None, "lon": None, "city_name": None}
    return {"lat": row[0], "lon": row[1], "city_name": row[2]}
