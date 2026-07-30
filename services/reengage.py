from __future__ import annotations

import asyncio
import logging
from datetime import UTC, datetime, timedelta

from aiogram import Bot
from aiogram.exceptions import TelegramAPIError
from sqlalchemy import select, update

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
            select(
                User.tg_id,
                User.last_activity_at,
                User.reengage_level,
                User.language,
            ).where(
                User.last_activity_at <= now - timedelta(days=1),
                User.reengage_level < 3,
                User.is_test.is_(False),
                User.tg_id > 0,
                User.is_blocked.is_(False),
            )
        )
        candidates = [
            (tg_id, last_activity_at, reengage_level, language or "ru")
            for tg_id, last_activity_at, reengage_level, language in result.all()
        ]

    for tg_id, last_activity_at, reengage_level, lang in candidates:
        assert last_activity_at is not None
        idle = _idle_days(last_activity_at, now)
        next_level = None
        for days, level in REENGAGE_STEPS:
            if idle >= days and reengage_level < level:
                next_level = level
        if next_level is None:
            continue

        # Reserve this step before sending so parallel bot instances cannot
        # deliver the same re-engagement notification.
        async with async_session_maker() as session:
            reserved = await session.execute(
                update(User)
                .where(
                    User.tg_id == tg_id,
                    User.reengage_level == reengage_level,
                    User.last_activity_at == last_activity_at,
                    User.is_blocked.is_(False),
                )
                .values(reengage_level=next_level)
                .returning(User.tg_id)
            )
            if reserved.scalar_one_or_none() is None:
                await session.rollback()
                continue
            await session.commit()

        try:
            await bot.send_message(
                tg_id,
                t("reengage_search", lang),
                reply_markup=main_menu_kb(lang),
            )
        except TelegramAPIError:
            logger.info("reengage skip chat %s", tg_id)
        except Exception:
            logger.exception("reengage failed for %s", tg_id)
        else:
            continue

        # A transient Telegram failure should not permanently consume a step.
        async with async_session_maker() as session:
            await session.execute(
                update(User)
                .where(
                    User.tg_id == tg_id,
                    User.reengage_level == next_level,
                    User.last_activity_at == last_activity_at,
                )
                .values(reengage_level=reengage_level)
            )
            await session.commit()


async def reengage_loop(bot: Bot) -> None:
    await asyncio.sleep(30)
    while True:
        try:
            await process_reengage(bot)
        except Exception:
            logger.exception("reengage loop error")
        await asyncio.sleep(POLL_SECONDS)
