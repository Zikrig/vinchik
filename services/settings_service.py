from __future__ import annotations

from time import monotonic

from sqlalchemy import select, text
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from config import settings
from database.models import PremiumPlan, Setting


DEFAULTS = {
    "daily_like_limit": str(settings.default_daily_like_limit),
    "max_distance_km": str(settings.default_max_distance_km),
    "profile_reshow_days": str(settings.default_profile_reshow_days),
    "registration_only": "true" if settings.registration_only_default else "false",
    "test_users_visible": "true",
    "manager_contact": settings.manager_contact,
    "payment_card": settings.payment_card or "укажите карту в админке",
    "payment_check_time": settings.payment_check_time,
}

_CACHE_TTL_SECONDS = 30.0
_setting_cache: dict[str, tuple[float, str]] = {}

# bot and web seed defaults at the same time; without this both can insert plans
_SEED_LOCK_ID = 872314206


async def ensure_defaults(session: AsyncSession) -> None:
    await session.execute(
        text("SELECT pg_advisory_xact_lock(:lock_id)"), {"lock_id": _SEED_LOCK_ID}
    )
    await session.execute(
        insert(Setting)
        .values([{"key": key, "value": value} for key, value in DEFAULTS.items()])
        .on_conflict_do_nothing(index_elements=[Setting.key])
    )
    result = await session.execute(select(PremiumPlan))
    if not result.scalars().first():
        session.add_all(
            [
                PremiumPlan(title="2 дня", days=2, price_text="уточняйте"),
                PremiumPlan(title="7 дней", days=7, price_text="уточняйте"),
                PremiumPlan(title="30 дней", days=30, price_text="уточняйте"),
            ]
        )
    await session.commit()


async def get_setting(session: AsyncSession, key: str, default: str | None = None) -> str:
    cached = _setting_cache.get(key)
    now = monotonic()
    if cached is not None and cached[0] > now:
        return cached[1]

    row = await session.get(Setting, key)
    if row:
        value = row.value
    elif key in DEFAULTS:
        value = DEFAULTS[key]
    else:
        return default or ""
    _setting_cache[key] = (now + _CACHE_TTL_SECONDS, value)
    return value


async def set_setting(session: AsyncSession, key: str, value: str) -> None:
    row = await session.get(Setting, key)
    if row is None:
        session.add(Setting(key=key, value=value))
    else:
        row.value = value
    await session.commit()
    _setting_cache[key] = (monotonic() + _CACHE_TTL_SECONDS, value)


async def get_daily_like_limit(session: AsyncSession) -> int:
    return int(await get_setting(session, "daily_like_limit", "50"))


async def get_max_distance_km(session: AsyncSession) -> float:
    return float(await get_setting(session, "max_distance_km", "20000"))


async def get_profile_reshow_days(session: AsyncSession) -> int:
    """0 = never reshow rated profiles; default 60 (~2 months)."""
    raw = await get_setting(
        session, "profile_reshow_days", str(settings.default_profile_reshow_days)
    )
    try:
        return max(0, int(raw))
    except ValueError:
        return settings.default_profile_reshow_days


async def is_registration_only(session: AsyncSession) -> bool:
    return (await get_setting(session, "registration_only", "true")).lower() in {
        "1",
        "true",
        "yes",
        "on",
    }


async def are_test_users_visible_setting(session: AsyncSession) -> bool:
    return (await get_setting(session, "test_users_visible", "true")).lower() in {
        "1",
        "true",
        "yes",
        "on",
    }


async def get_manager_contact(session: AsyncSession) -> str:
    return await get_setting(session, "manager_contact", settings.manager_contact)


async def get_payment_card(session: AsyncSession) -> str:
    return await get_setting(session, "payment_card", settings.payment_card)


async def get_payment_check_time(session: AsyncSession) -> str:
    return await get_setting(session, "payment_check_time", settings.payment_check_time)


async def get_payment_info(session: AsyncSession) -> dict[str, str]:
    return {
        "manager": await get_manager_contact(session),
        "card": await get_payment_card(session),
        "check_time": await get_payment_check_time(session),
    }
