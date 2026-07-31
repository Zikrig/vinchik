from __future__ import annotations

from datetime import UTC, datetime, timedelta

from sqlalchemy import String, and_, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from sqlalchemy import delete

from database.models import DailyLikeStat, Gender, Like, LookingFor, Profile, User
from services.media import set_profile_photos
from services.users import load_user_with_profile

MAP_MARKERS_LIMIT = 200

# Admin "снять премиум" — дата в прошлом: is_premium() = false, поле не пустое.
PREMIUM_REVOKED_UNTIL = datetime(2004, 1, 1, 0, 0, tzinfo=UTC)


def parse_premium_until(raw: str) -> datetime | None:
    """Parse admin date into UTC. None = invalid (caller decides empty → revoke)."""
    text = (raw or "").strip()
    if not text:
        return None
    text = text.replace("T", " ").replace(",", ".")
    # DD.MM.YYYY[ HH:MM]
    for fmt in (
        "%Y-%m-%d %H:%M:%S",
        "%Y-%m-%d %H:%M",
        "%Y-%m-%d",
        "%d.%m.%Y %H:%M:%S",
        "%d.%m.%Y %H:%M",
        "%d.%m.%Y",
        "%d/%m/%Y %H:%M",
        "%d/%m/%Y",
    ):
        try:
            dt = datetime.strptime(text, fmt)
            return dt.replace(tzinfo=UTC)
        except ValueError:
            continue
    try:
        dt = datetime.fromisoformat(text)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=UTC)
        else:
            dt = dt.astimezone(UTC)
        return dt
    except ValueError:
        return None


def _parse_bool(raw: str | None) -> bool | None:
    if raw is None or raw == "" or raw == "any":
        return None
    return raw.lower() in {"1", "true", "yes", "on"}


def _as_str(col):
    return func.cast(col, String)


async def search_accounts(
    session: AsyncSession,
    *,
    q: str = "",
    is_test: bool | None = None,
    is_blocked: bool | None = None,
    is_active: bool | None = None,
    is_complete: bool | None = None,
    gender: str | None = None,
    looking_for: str | None = None,
    language: str | None = None,
    has_premium: bool | None = None,
    limit: int = 200,
) -> list[tuple[User, Profile | None]]:
    stmt = (
        select(User)
        .outerjoin(Profile, Profile.user_id == User.tg_id)
        .options(selectinload(User.profile))
        .order_by(User.created_at.desc())
        .limit(limit)
    )
    filters = []

    if is_test is not None:
        filters.append(User.is_test.is_(is_test))
    if is_blocked is not None:
        filters.append(User.is_blocked.is_(is_blocked))
    if is_active is not None:
        filters.append(Profile.is_active.is_(is_active))
    if is_complete is not None:
        filters.append(Profile.is_complete.is_(is_complete))
    if gender in {"male", "female"}:
        filters.append(Profile.gender == Gender(gender))
    if looking_for in {"male", "female"}:
        filters.append(Profile.looking_for == LookingFor(looking_for))
    elif looking_for == "all":
        filters.append(Profile.looking_for == LookingFor.any)
    if language in {"ru", "tg"}:
        filters.append(User.language == language)
    if has_premium is not None:
        now = datetime.now(UTC)
        if has_premium:
            filters.append(and_(User.premium_until.is_not(None), User.premium_until > now))
        else:
            filters.append(or_(User.premium_until.is_(None), User.premium_until <= now))

    q = (q or "").strip()
    if q:
        like = f"%{q}%"
        parts = [
            User.username.ilike(like),
            Profile.name.ilike(like),
            Profile.city_name.ilike(like),
            Profile.description.ilike(like),
            User.language.ilike(like),
            _as_str(Profile.gender).ilike(like),
            _as_str(Profile.looking_for).ilike(like),
            _as_str(User.tg_id).ilike(like),
            _as_str(Profile.age).ilike(like),
        ]
        if q.lstrip("-").isdigit():
            parts.append(User.tg_id == int(q))
            parts.append(Profile.age == int(q))
        filters.append(or_(*parts))

    if filters:
        stmt = stmt.where(and_(*filters))

    result = await session.execute(stmt)
    users = list(result.scalars().unique().all())
    return [(u, u.profile) for u in users]


async def map_markers(
    session: AsyncSession,
    *,
    admin_ids: set[int],
    limit: int = MAP_MARKERS_LIMIT,
) -> list[dict]:
    """Up to `limit` profiles with coordinates.

    Admins with geo are always included first; the rest are a random sample
    (not “latest N”), so the map is not biased to recent registrations.
    """
    limit = max(1, int(limit))
    geo = and_(Profile.lat.is_not(None), Profile.lon.is_not(None))

    admins: list[User] = []
    if admin_ids:
        admin_result = await session.execute(
            select(User)
            .join(Profile, Profile.user_id == User.tg_id)
            .where(User.tg_id.in_(admin_ids), geo)
            .options(selectinload(User.profile))
        )
        admins = list(admin_result.scalars().unique().all())

    remaining = max(0, limit - len(admins))
    others: list[User] = []
    if remaining:
        others_stmt = (
            select(User)
            .join(Profile, Profile.user_id == User.tg_id)
            .where(geo)
            .options(selectinload(User.profile))
            .order_by(func.random())
            .limit(remaining)
        )
        if admin_ids:
            others_stmt = others_stmt.where(User.tg_id.notin_(admin_ids))
        others_result = await session.execute(others_stmt)
        others = list(others_result.scalars().unique().all())

    markers: list[dict] = []
    for u in admins + others:
        p = u.profile
        if not p or p.lat is None or p.lon is None:
            continue
        markers.append(
            {
                "tg_id": u.tg_id,
                "username": u.username,
                "name": p.name,
                "age": p.age,
                "city": p.city_name,
                "lat": p.lat,
                "lon": p.lon,
                "is_admin": u.tg_id in admin_ids,
                "is_test": u.is_test,
            }
        )
    return markers


def filters_from_query(params) -> dict:
    return {
        "q": (params.get("q") or "").strip(),
        "is_test": _parse_bool(params.get("is_test")),
        "is_blocked": _parse_bool(params.get("is_blocked")),
        "is_active": _parse_bool(params.get("is_active")),
        "is_complete": _parse_bool(params.get("is_complete")),
        "gender": params.get("gender") or None,
        "looking_for": params.get("looking_for") or None,
        "language": params.get("language") or None,
        "has_premium": _parse_bool(params.get("has_premium")),
    }


async def account_like_stats(session: AsyncSession, user_id: int) -> dict[str, int]:
    from services.limits import utc_today

    sent = await session.execute(
        select(func.count()).select_from(Like).where(Like.from_user_id == user_id)
    )
    received = await session.execute(
        select(func.count()).select_from(Like).where(Like.to_user_id == user_id)
    )
    today = await session.execute(
        select(func.coalesce(func.sum(DailyLikeStat.count), 0)).where(
            DailyLikeStat.user_id == user_id,
            DailyLikeStat.utc_date == utc_today(),
        )
    )
    all_days = await session.execute(
        select(func.coalesce(func.sum(DailyLikeStat.count), 0)).where(
            DailyLikeStat.user_id == user_id
        )
    )
    return {
        "sent": int(sent.scalar_one()),
        "received": int(received.scalar_one()),
        "daily_used": int(today.scalar_one() or 0),
        "daily_all_days": int(all_days.scalar_one()),
    }


async def clear_user_likes(
    session: AsyncSession,
    user_id: int,
    *,
    sent: bool = True,
    received: bool = True,
    daily_stats: bool = True,
) -> int:
    if not sent and not received and not daily_stats:
        return 0
    n = 0
    if sent:
        r = await session.execute(delete(Like).where(Like.from_user_id == user_id))
        n += r.rowcount or 0
    if received:
        r = await session.execute(delete(Like).where(Like.to_user_id == user_id))
        n += r.rowcount or 0
    if daily_stats:
        r = await session.execute(delete(DailyLikeStat).where(DailyLikeStat.user_id == user_id))
        n += r.rowcount or 0
    if sent or received:
        user = await session.get(User, user_id)
        if user is not None:
            user.likes_notify_message_id = None
            user.last_like_notify_at = None
    await session.commit()
    return n


async def set_account_premium(
    session: AsyncSession,
    user_id: int,
    *,
    premium_until_raw: str | None = None,
    clear: bool = False,
    add_days: int | None = None,
) -> User | None:
    user = await session.get(User, user_id)
    if user is None:
        return None

    now = datetime.now(UTC)
    if clear:
        user.premium_until = PREMIUM_REVOKED_UNTIL
    elif add_days is not None and add_days > 0:
        base = user.premium_until
        if base is not None and base.tzinfo is None:
            base = base.replace(tzinfo=UTC)
        if base is None or base < now:
            base = now
        user.premium_until = base + timedelta(days=int(add_days))
    elif premium_until_raw is not None:
        raw = (premium_until_raw or "").strip()
        if not raw:
            user.premium_until = PREMIUM_REVOKED_UNTIL
        else:
            dt = parse_premium_until(raw)
            if dt is None:
                return None
            user.premium_until = dt

    await session.commit()
    return await load_user_with_profile(session, user_id)


async def update_account_user(
    session: AsyncSession,
    user_id: int,
    *,
    is_test: bool,
    is_blocked: bool,
    reengage_level: int,
    is_suspicious: bool | None = None,
    suspicious_reason: str | None = None,
) -> User | None:
    user = await load_user_with_profile(session, user_id)
    if user is None:
        return None

    # username is read-only in admin; language lives on the profile form
    user.is_test = is_test
    user.reengage_level = max(0, min(3, int(reengage_level)))

    if is_blocked and not user.is_blocked:
        user.is_blocked = True
        user.blocked_at = datetime.now(UTC)
    elif not is_blocked and user.is_blocked:
        user.is_blocked = False
        user.blocked_at = None

    if is_suspicious is not None:
        if is_suspicious and not user.is_suspicious:
            user.is_suspicious = True
            user.suspicious_at = datetime.now(UTC)
            user.suspicious_reason = (suspicious_reason or "").strip() or "вручную"
        elif is_suspicious and user.is_suspicious:
            if suspicious_reason is not None:
                user.suspicious_reason = (suspicious_reason or "").strip() or user.suspicious_reason
        elif not is_suspicious:
            user.is_suspicious = False
            user.suspicious_reason = None
            user.suspicious_at = None

    if user.is_blocked and user.profile and user.profile.is_active:
        user.profile.is_active = False

    await session.commit()
    return await load_user_with_profile(session, user_id)


async def update_account_profile(
    session: AsyncSession,
    user_id: int,
    *,
    name: str | None,
    age: int | None,
    city_name: str | None,
    lat: float | None,
    lon: float | None,
    gender: str | None,
    looking_for: str | None,
    description: str | None,
    photo_file_id: str | None,
    is_active: bool,
    is_complete: bool,
    clear_photo: bool = False,
    language: str | None = None,
) -> User | None:
    user = await load_user_with_profile(session, user_id)
    if user is None:
        return None

    if user.profile is None:
        session.add(Profile(user_id=user.tg_id))
        await session.flush()
        user = await load_user_with_profile(session, user_id)
        assert user and user.profile

    p = user.profile
    assert p is not None
    p.name = (name or "").strip() or None
    p.age = age
    p.city_name = (city_name or "").strip() or None
    p.lat = lat
    p.lon = lon
    p.description = (description or "").strip() or None
    p.is_active = is_active
    p.is_complete = is_complete
    if clear_photo:
        set_profile_photos(p, [])
    elif photo_file_id is not None:
        # The form exposes only the first id — keep the rest of the album.
        raw = photo_file_id.strip()
        rest = [str(x) for x in (p.photo_file_ids or [])[1:] if x]
        set_profile_photos(p, ([raw] if raw else []) + rest)

    if gender in {"male", "female"}:
        p.gender = Gender(gender)
    # empty / omitted — keep current (cannot clear via admin)

    if looking_for in {"male", "female", "any"}:
        p.looking_for = LookingFor(looking_for)
    # empty / omitted — keep current (cannot clear via admin)

    if language in {"ru", "tg"}:
        user.language = language
        user.language_chosen = True

    if user.is_blocked and p.is_active:
        p.is_active = False

    await session.commit()
    return await load_user_with_profile(session, user_id)
