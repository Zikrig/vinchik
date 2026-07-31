from __future__ import annotations

import math
import random
from datetime import UTC, datetime

from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from config import settings
from database.models import Gender, LookingFor, Profile, User
from services.media import (
    TEST_PHOTO_MARKER,
    list_test_photo_markers,
    local_photo_path,
)
from services.users import get_or_create_user

# Dushanbe, capital of Tajikistan
DUSHANBE_LAT = 38.5598
DUSHANBE_LON = 68.7870
DUSHANBE_CITY = "Душанбе"

# Spawn test profiles near center (not across whole TJ).
TEST_SPAWN_RADIUS_KM = 60.0
# Prefer not placing two profiles with the same photo closer than this.
TEST_SAME_PHOTO_MIN_KM = 8.0
_TEST_PHOTO_PLACE_ATTEMPTS = 24

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
    radius_km = max(0.1, float(radius_km))
    r = radius_km * math.sqrt(random.random())
    theta = random.uniform(0, 2 * math.pi)
    dlat = (r * math.cos(theta)) / 111.0
    cos_lat = math.cos(math.radians(lat)) or 1e-6
    dlon = (r * math.sin(theta)) / (111.0 * cos_lat)
    return round(lat + dlat, 6), round(lon + dlon, 6)


def _haversine_km(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    r = 6371.0
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlmb = math.radians(lon2 - lon1)
    a = (
        math.sin(dphi / 2) ** 2
        + math.cos(p1) * math.cos(p2) * math.sin(dlmb / 2) ** 2
    )
    return 2 * r * math.asin(min(1.0, math.sqrt(a)))


def _min_distance_km(
    lat: float, lon: float, points: list[tuple[float, float]]
) -> float:
    if not points:
        return float("inf")
    return min(_haversine_km(lat, lon, plat, plon) for plat, plon in points)


def _pick_test_photo(
    gender: Gender,
    lat: float,
    lon: float,
    placements: dict[str, list[tuple[float, float]]],
) -> str | None:
    """Choose a gender photo maximizing distance to others with the same file."""
    markers = list_test_photo_markers(gender)
    if not markers:
        if local_photo_path(TEST_PHOTO_MARKER) is not None:
            return TEST_PHOTO_MARKER
        return None

    best_marker = markers[0]
    best_score = -1.0
    # Shuffle so ties don't always pick the same file.
    for marker in random.sample(markers, k=len(markers)):
        score = _min_distance_km(lat, lon, placements.get(marker, []))
        # Light preference for less-used photos when distances are similar.
        usage = len(placements.get(marker, []))
        score -= usage * 0.05
        if score > best_score:
            best_score = score
            best_marker = marker
    return best_marker


def _place_test_profile(
    gender: Gender,
    center_lat: float,
    center_lon: float,
    placements: dict[str, list[tuple[float, float]]],
    radius_km: float = TEST_SPAWN_RADIUS_KM,
) -> tuple[float, float, str | None]:
    """Random geo + photo; retry so same photo stays farther apart when possible."""
    markers = list_test_photo_markers(gender)
    fallback = (
        TEST_PHOTO_MARKER
        if local_photo_path(TEST_PHOTO_MARKER) is not None
        else None
    )
    if not markers and fallback is None:
        lat, lon = _random_near(center_lat, center_lon, radius_km)
        return lat, lon, None

    best: tuple[float, float, str | None, float] | None = None
    for _ in range(_TEST_PHOTO_PLACE_ATTEMPTS):
        lat, lon = _random_near(center_lat, center_lon, radius_km)
        photo = _pick_test_photo(gender, lat, lon, placements)
        if photo is None:
            return lat, lon, None
        dist = _min_distance_km(lat, lon, placements.get(photo, []))
        if best is None or dist > best[3]:
            best = (lat, lon, photo, dist)
        if dist >= TEST_SAME_PHOTO_MIN_KM:
            break
    assert best is not None
    return best[0], best[1], best[2]


async def _existing_test_photo_placements(
    session: AsyncSession,
) -> dict[str, list[tuple[float, float]]]:
    result = await session.execute(
        select(Profile.photo_file_id, Profile.lat, Profile.lon)
        .join(User, User.tg_id == Profile.user_id)
        .where(
            User.is_test.is_(True),
            Profile.photo_file_id.is_not(None),
            Profile.lat.is_not(None),
            Profile.lon.is_not(None),
        )
    )
    out: dict[str, list[tuple[float, float]]] = {}
    for photo_id, lat, lon in result.all():
        if not photo_id:
            continue
        out.setdefault(str(photo_id), []).append((float(lat), float(lon)))
    return out


async def test_spawn_center(session: AsyncSession) -> tuple[float, float, str]:
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


async def create_test_users(
    session: AsyncSession,
    count: int,
    radius_km: float | None = None,
    center_lat: float | None = None,
    center_lon: float | None = None,
    city_name: str | None = None,
) -> int:
    count = max(0, min(int(count), 1000))
    spawn_r = (
        TEST_SPAWN_RADIUS_KM
        if radius_km is None
        else max(1.0, min(float(radius_km), 500.0))
    )
    from services.settings_service import are_test_users_visible_setting

    visible = await are_test_users_visible_setting(session)
    if center_lat is not None and center_lon is not None:
        lat0 = max(-90.0, min(90.0, float(center_lat)))
        lon0 = max(-180.0, min(180.0, float(center_lon)))
        city = (city_name or "").strip() or "Карта"
        center_lat, center_lon, city = lat0, lon0, city
    else:
        center_lat, center_lon, city = await test_spawn_center(session)
    placements = await _existing_test_photo_placements(session)
    created = 0
    for _ in range(count):
        tg_id = await _next_test_tg_id(session)
        gender = random.choice((Gender.male, Gender.female))
        lat, lon, test_photo = _place_test_profile(
            gender, center_lat, center_lon, placements, spawn_r
        )
        if gender == Gender.male:
            name = random.choice(_TEST_NAMES_MALE)
        else:
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
            looking_for=LookingFor.any,
            lat=lat,
            lon=lon,
            city_name=city,
            description="",
            photo_file_id=test_photo,
            photo_file_ids=[test_photo] if test_photo else None,
            is_active=visible,
            is_complete=True,
        )
        session.add(user)
        session.add(profile)
        await session.flush()
        if test_photo is not None:
            placements.setdefault(test_photo, []).append((lat, lon))
        created += 1
    await session.commit()
    return created


async def count_test_users(session: AsyncSession) -> int:
    result = await session.execute(
        select(func.count()).select_from(User).where(User.is_test.is_(True))
    )
    return int(result.scalar_one())


async def count_profiles_by_gender(session: AsyncSession) -> dict[str, int]:
    """All profiles with gender set (including test). total = male + female."""
    result = await session.execute(
        select(Profile.gender, func.count())
        .where(Profile.gender.is_not(None))
        .group_by(Profile.gender)
    )
    males = 0
    females = 0
    for gender, n in result.all():
        if gender == Gender.male:
            males = int(n)
        elif gender == Gender.female:
            females = int(n)
    return {"male": males, "female": females, "total": males + females}


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
