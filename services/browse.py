from __future__ import annotations

from datetime import UTC, datetime, timedelta

from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from database.models import Gender, Like, LookingFor, Profile, User
from services.geo import haversine_km

# Implicit for viewer: expand only through these tiers, then stop at max_distance_km.
RADIUS_TIERS_KM = (
    10,
    25,
    50,
    100,
    250,
    500,
    1000,
    2500,
    5000,
    10000,
    20000,
)


def profile_caption(profile: Profile) -> str:
    name = profile.name or "?"
    age = profile.age or "?"
    city = profile.city_name or "?"
    desc = (profile.description or "").strip()
    head = f"{name}, {age}, {city}"
    return f"{head}\n{desc}" if desc else head


def _looking_matches(looking: LookingFor, gender: Gender) -> bool:
    if looking == LookingFor.any:
        return True
    return looking.value == gender.value


def recently_rated_subquery(viewer_tg_id: int, reshow_days: int, now: datetime):
    """Pairs hidden from feed. Sleep does not create a Like — only like/dislike/message do.

    reshow_days == 0 → hide forever after any rating.
    reshow_days > 0 → hide only while Like.created_at is within the window.
    """
    q = select(Like.to_user_id).where(Like.from_user_id == viewer_tg_id)
    if reshow_days > 0:
        cutoff = now - timedelta(days=reshow_days)
        q = q.where(Like.created_at >= cutoff)
    return q


async def next_profile(
    session: AsyncSession,
    viewer: User,
    viewer_profile: Profile,
) -> Profile | None:
    """Nearest available profile: expand through radius tiers up to max_distance_km."""
    from services.settings_service import get_max_distance_km, get_profile_reshow_days

    if (
        viewer_profile.lat is None
        or viewer_profile.lon is None
        or viewer_profile.gender is None
        or viewer_profile.looking_for is None
    ):
        return None

    max_km = await get_max_distance_km(session)
    reshow_days = await get_profile_reshow_days(session)
    tiers = [r for r in RADIUS_TIERS_KM if r <= max_km]
    if not tiers and max_km > 0:
        tiers = [max_km]
    elif tiers and tiers[-1] < max_km:
        tiers.append(max_km)

    now = datetime.now(UTC)
    rated = recently_rated_subquery(viewer.tg_id, reshow_days, now)

    gender_filter = True
    if viewer_profile.looking_for == LookingFor.male:
        gender_filter = Profile.gender == Gender.male
    elif viewer_profile.looking_for == LookingFor.female:
        gender_filter = Profile.gender == Gender.female

    looking_back = or_(
        Profile.looking_for == LookingFor.any,
        Profile.looking_for == LookingFor(viewer_profile.gender.value),
    )

    q = (
        select(Profile)
        .join(User, User.tg_id == Profile.user_id)
        .where(
            Profile.is_active.is_(True),
            Profile.is_complete.is_(True),
            User.is_blocked.is_(False),
            Profile.user_id != viewer.tg_id,
            Profile.user_id.not_in(rated),
            Profile.lat.is_not(None),
            Profile.lon.is_not(None),
            gender_filter,
            looking_back,
        )
        .options(selectinload(Profile.user))
        .order_by(
            (User.premium_until.is_not(None) & (User.premium_until > now)).desc(),
            func.random(),
        )
        .limit(1000)
    )
    result = await session.execute(q)
    candidates = list(result.scalars().all())

    assert viewer_profile.lat is not None and viewer_profile.lon is not None
    with_dist: list[tuple[float, Profile]] = []
    for p in candidates:
        if not p.gender or not _looking_matches(viewer_profile.looking_for, p.gender):
            continue
        assert p.lat is not None and p.lon is not None
        d = haversine_km(viewer_profile.lat, viewer_profile.lon, p.lat, p.lon)
        with_dist.append((d, p))

    for radius in tiers:
        in_tier = [p for d, p in with_dist if d <= radius]
        if in_tier:
            return in_tier[0]
    return None
