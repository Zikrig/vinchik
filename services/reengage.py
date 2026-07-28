from __future__ import annotations

import asyncio
import logging
from datetime import UTC, datetime, timedelta

from aiogram import Bot
from aiogram.exceptions import TelegramAPIError
from sqlalchemy import select

from database.models import User
from database.session import async_session_maker
from keyboards.inline import main_menu_kb
from locales import t

logger = logging.getLogger(__name__)

# (min idle days, level to set after send)
REENGAGE_STEPS = (
    (1, 1),
    (3, 2),
    (7, 3),
)

POLL_SECONDS = 3600


def _idle_days(last: datetime, now: datetime) -> float:
    if last.tzinfo is None:
        last = last.replace(tzinfo=UTC)
    return (now - last).total_seconds() / 86400


async def process_reengage(bot: Bot) -> None:
    now = datetime.now(UTC)
    async with async_session_maker() as session:
        result = await session.execute(
            select(User).where(
                User.last_activity_at.is_not(None),
                User.reengage_level < 3,
                User.is_test.is_(False),
                User.tg_id > 0,
                User.is_blocked.is_(False),
            )
        )
        # Snapshot scalars — do not touch ORM after rollback/commit of other rows.
        candidates = [
            (u.tg_id, u.last_activity_at, u.reengage_level, u.language or "ru")
            for u in result.scalars().all()
        ]

    for tg_id, last_activity_at, reengage_level, lang in candidates:
        assert last_activity_at is not None
        idle = _idle_days(last_activity_at, now)
        next_level = None
        for days, level in REENGAGE_STEPS:
            if idle >= days and reengage_level < level:
                next_level = level
                break
        if next_level is None:
            continue

        try:
            await bot.send_message(
                tg_id,
                t("reengage_search", lang),
                reply_markup=main_menu_kb(lang),
            )
        except TelegramAPIError:
            logger.info("reengage skip chat %s", tg_id)
            continue
        except Exception:
            logger.exception("reengage failed for %s", tg_id)
            continue

        async with async_session_maker() as session:
            user = await session.get(User, tg_id)
            if user is None:
                continue
            user.reengage_level = next_level
            await session.commit()


async def reengage_loop(bot: Bot) -> None:
    await asyncio.sleep(30)
    while True:
        try:
            await process_reengage(bot)
        except Exception:
            logger.exception("reengage loop error")
        await asyncio.sleep(POLL_SECONDS)
