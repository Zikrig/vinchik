from __future__ import annotations

import html
import math
from datetime import UTC, datetime, timedelta

from sqlalchemy import and_, case, func, literal, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from database.models import Gender, Like, LookingFor, Profile, User
from services.performance import timed

# Distance expansion (km), capped by max_distance_km.
RADIUS_TIERS_KM = (
    10,
    25,
    50,
    100,
    250,
    500,
)

# Age bands paired with radius tiers on a diagonal wave (see next_profile).
AGE_TOLERANCE_YEARS = (2, 5, 10, 999)


def profile_caption(profile: Profile) -> str:
    name = html.escape(profile.name or "?")
    age = profile.age or "?"
    city = html.escape(profile.city_name or "?")
    desc = html.escape((profile.description or "").strip())
    head = f"{name}, {age}, {city}"
    return f"{head}\n{desc}" if desc else head


@timed("browse.next_profile")
async def next_profile(
    session: AsyncSession,
    viewer: User,
    viewer_profile: Profile,
) -> Profile | None:
    """Diagonal expansion of distance × age tolerance.

    Wave 0: 10 km ±2
    Wave 1: 10 km ±5  and  25 km ±2
    Wave 2: 10 km ±10 and  25 km ±5  and  50 km ±2
    … (radius_index + age_index == wave)

    Each call re-evaluates from scratch over current candidates (no sticky
    “mode”). New nearby peers of a close age win on the next card.

    Within a wave: premium → closer age → closer km.
    """
    from services.settings_service import get_max_distance_km, get_profile_reshow_days

    if (
        viewer_profile.lat is None
        or viewer_profile.lon is None
        or viewer_profile.gender is None
        or viewer_profile.looking_for is None
    ):
        return None

    max_km = await get_max_distance_km(session)
    tiers = [r for r in RADIUS_TIERS_KM if r <= max_km]
    if not tiers and max_km > 0:
        tiers = [max_km]
    elif tiers and tiers[-1] < max_km:
        tiers.append(max_km)

    reshow_days = await get_profile_reshow_days(session)
    since = (
        None
        if reshow_days <= 0
        else datetime.now(UTC) - timedelta(days=reshow_days)
    )
    outgoing = select(Like.id).where(
        Like.from_user_id == viewer.tg_id,
        Like.to_user_id == Profile.user_id,
    )
    incoming = select(Like.id).where(
        Like.to_user_id == viewer.tg_id,
        Like.from_user_id == Profile.user_id,
    )
    if since is not None:
        outgoing = outgoing.where(Like.created_at >= since)
        incoming = incoming.where(Like.created_at >= since)

    gender_filter = True
    if viewer_profile.looking_for == LookingFor.male:
        gender_filter = Profile.gender == Gender.male
    elif viewer_profile.looking_for == LookingFor.female:
        gender_filter = Profile.gender == Gender.female

    looking_back = or_(
        Profile.looking_for == LookingFor.any,
        Profile.looking_for == LookingFor(viewer_profile.gender.value),
    )

    lat = float(viewer_profile.lat)
    lon = float(viewer_profile.lon)
    # Cheap indexed rectangle first; exact great-circle distance stays below.
    lat_delta = min(180.0, max_km / 111.045)
    lon_delta = min(
        180.0,
        max_km / (111.045 * max(abs(math.cos(math.radians(lat))), 1e-6)),
    )
    lat_bounds = Profile.lat.between(max(-90.0, lat - lat_delta), min(90.0, lat + lat_delta))
    lon_min, lon_max = lon - lon_delta, lon + lon_delta
    if lon_min < -180.0:
        lon_bounds = or_(Profile.lon >= lon_min + 360.0, Profile.lon <= lon_max)
    elif lon_max > 180.0:
        lon_bounds = or_(Profile.lon >= lon_min, Profile.lon <= lon_max - 360.0)
    else:
        lon_bounds = Profile.lon.between(lon_min, lon_max)
    dot = (
        func.sin(func.radians(lat)) * func.sin(func.radians(Profile.lat))
        + func.cos(func.radians(lat))
        * func.cos(func.radians(Profile.lat))
        * func.cos(func.radians(Profile.lon - lon))
    )
    distance_km = 6371.0088 * func.acos(
        func.least(1.0, func.greatest(-1.0, dot))
    )
    age_diff = (
        func.coalesce(func.abs(Profile.age - viewer_profile.age), 99)
        if viewer_profile.age is not None
        else literal(99)
    )
    premium_rank = case(
        (
            and_(
                User.premium_until.is_not(None),
                User.premium_until > datetime.now(UTC),
            ),
            0,
        ),
        else_=1,
    )

    base_q = (
        select(Profile)
        .join(User, User.tg_id == Profile.user_id)
        .where(
            Profile.is_active.is_(True),
            Profile.is_complete.is_(True),
            User.is_blocked.is_(False),
            Profile.user_id != viewer.tg_id,
            ~outgoing.exists(),
            ~incoming.exists(),
            Profile.lat.is_not(None),
            Profile.lon.is_not(None),
            lat_bounds,
            lon_bounds,
            gender_filter,
            looking_back,
        )
    )

    n_r = len(tiers)
    n_a = len(AGE_TOLERANCE_YEARS)
    wave_whens = []
    for wave in range(n_r + n_a - 1):
        wave_conditions = []
        for ri, radius in enumerate(tiers):
            ai = wave - ri
            if ai < 0 or ai >= n_a:
                continue
            age_tol = AGE_TOLERANCE_YEARS[ai]
            wave_conditions.append(
                and_(distance_km <= radius, age_diff <= age_tol)
            )
        if not wave_conditions:
            continue
        wave_whens.append((or_(*wave_conditions), wave))

    if not wave_whens:
        return None
    wave_rank = case(*wave_whens, else_=n_r + n_a)
    result = await session.execute(
        base_q.where(distance_km <= max_km, wave_rank < n_r + n_a)
        .order_by(wave_rank, premium_rank, age_diff, distance_km)
        .limit(1)
    )
    return result.scalar_one_or_none()
