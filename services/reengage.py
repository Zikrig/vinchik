from __future__ import annotations

import asyncio
import logging
from datetime import UTC, datetime, timedelta

from aiogram import Bot
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup
from sqlalchemy import select

from config import settings
from database.models import User
from database.session import async_session_maker
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
            )
        )
        users = list(result.scalars().all())
        for user in users:
            assert user.last_activity_at is not None
            idle = _idle_days(user.last_activity_at, now)
            next_level = None
            for days, level in REENGAGE_STEPS:
                if idle >= days and user.reengage_level < level:
                    next_level = level
                    break
            if next_level is None:
                continue
            lang = user.language or "ru"
            kb = InlineKeyboardMarkup(
                inline_keyboard=[
                    [
                        InlineKeyboardButton(
                            text=t("menu_browse", lang),
                            callback_data="browse:start",
                        )
                    ]
                ]
            )
            if settings.bot_username:
                kb.inline_keyboard.append(
                    [
                        InlineKeyboardButton(
                            text=t("menu_share", lang),
                            url=f"https://t.me/{settings.bot_username}",
                        )
                    ]
                )
            try:
                await bot.send_message(
                    user.tg_id,
                    t("reengage_search", lang),
                    reply_markup=kb,
                )
                user.reengage_level = next_level
                await session.commit()
            except Exception:
                logger.exception("reengage failed for %s", user.tg_id)
                await session.rollback()


async def reengage_loop(bot: Bot) -> None:
    await asyncio.sleep(30)
    while True:
        try:
            await process_reengage(bot)
        except Exception:
            logger.exception("reengage loop error")
        await asyncio.sleep(POLL_SECONDS)
