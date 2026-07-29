from __future__ import annotations

from datetime import UTC, datetime, timedelta

from sqlalchemy import func, or_, select, union
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from database.models import Gender, Like, LookingFor, Profile, User
from services.geo import haversine_km
from services.users import is_premium

# Distance expansion (km), capped by max_distance_km.
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

# Age bands paired with radius tiers on a diagonal wave (see next_profile).
AGE_TOLERANCE_YEARS = (2, 5, 10, 999)


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


def recently_rated_subquery(viewer_tg_id: int, since: datetime | None):
    """Hide the other user after like/dislike/message in either direction.

    Sleep does not create a Like. If ``since`` is set, only reactions at/after
    that time count (older pairs can appear again). If ``since`` is None
    (profile_reshow_days=0), hide forever.
    """
    outgoing = select(Like.to_user_id).where(Like.from_user_id == viewer_tg_id)
    incoming = select(Like.from_user_id).where(Like.to_user_id == viewer_tg_id)
    if since is not None:
        outgoing = outgoing.where(Like.created_at >= since)
        incoming = incoming.where(Like.created_at >= since)
    return union(outgoing, incoming)


def _age_diff(viewer_age: int | None, other_age: int | None) -> int:
    if viewer_age is None or other_age is None:
        return 99
    return abs(viewer_age - other_age)


def _pick_best(
    pool: list[tuple[float, Profile]],
    viewer_age: int | None,
) -> Profile:
    """Premium first, then smaller age gap, then closer distance."""

    def key(item: tuple[float, Profile]) -> tuple:
        dist, p = item
        premium = 0 if (p.user and is_premium(p.user)) else 1
        return (premium, _age_diff(viewer_age, p.age), dist)

    return min(pool, key=key)[1]


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
    rated = recently_rated_subquery(viewer.tg_id, since)

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
        .order_by(func.random())
        .limit(1000)
    )
    result = await session.execute(q)
    candidates = list(result.scalars().all())

    assert viewer_profile.lat is not None and viewer_profile.lon is not None
    viewer_age = viewer_profile.age
    with_dist: list[tuple[float, Profile]] = []
    for p in candidates:
        if not p.gender or not _looking_matches(viewer_profile.looking_for, p.gender):
            continue
        assert p.lat is not None and p.lon is not None
        d = haversine_km(viewer_profile.lat, viewer_profile.lon, p.lat, p.lon)
        with_dist.append((d, p))

    n_r = len(tiers)
    n_a = len(AGE_TOLERANCE_YEARS)
    for wave in range(n_r + n_a - 1):
        matched: list[tuple[float, Profile]] = []
        seen: set[int] = set()
        for ri, radius in enumerate(tiers):
            ai = wave - ri
            if ai < 0 or ai >= n_a:
                continue
            age_tol = AGE_TOLERANCE_YEARS[ai]
            for d, p in with_dist:
                if p.user_id in seen:
                    continue
                if d <= radius and _age_diff(viewer_age, p.age) <= age_tol:
                    matched.append((d, p))
                    seen.add(p.user_id)
        if matched:
            return _pick_best(matched, viewer_age)
    return None
