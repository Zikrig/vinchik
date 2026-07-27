from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy import String, and_, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from database.models import Gender, LookingFor, Profile, User


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
