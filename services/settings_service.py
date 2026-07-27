from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from config import settings
from database.models import PremiumPlan, Setting


DEFAULTS = {
    "daily_like_limit": str(settings.default_daily_like_limit),
    "max_distance_km": str(settings.default_max_distance_km),
    "registration_only": "true" if settings.registration_only_default else "false",
    "manager_contact": settings.manager_contact,
    "payment_card": settings.payment_card or "укажите карту в админке",
    "payment_check_time": settings.payment_check_time,
}


async def ensure_defaults(session: AsyncSession) -> None:
    for key, value in DEFAULTS.items():
        existing = await session.get(Setting, key)
        if existing is None:
            session.add(Setting(key=key, value=value))
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
    row = await session.get(Setting, key)
    if row:
        return row.value
    if key in DEFAULTS:
        return DEFAULTS[key]
    return default or ""


async def set_setting(session: AsyncSession, key: str, value: str) -> None:
    row = await session.get(Setting, key)
    if row is None:
        session.add(Setting(key=key, value=value))
    else:
        row.value = value
    await session.commit()


async def get_daily_like_limit(session: AsyncSession) -> int:
    return int(await get_setting(session, "daily_like_limit", "50"))


async def get_max_distance_km(session: AsyncSession) -> float:
    return float(await get_setting(session, "max_distance_km", "1000"))


async def is_registration_only(session: AsyncSession) -> bool:
    return (await get_setting(session, "registration_only", "true")).lower() in {
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
