from __future__ import annotations

from aiogram import Bot
from aiogram.enums import ChatMemberStatus
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from database.models import RequiredChannel, User
from services.users import is_premium


async def list_active_channels(session: AsyncSession) -> list[RequiredChannel]:
    result = await session.execute(
        select(RequiredChannel).where(RequiredChannel.is_active.is_(True))
    )
    return list(result.scalars().all())


async def user_subscribed_all(bot: Bot, session: AsyncSession, user: User) -> bool:
    if is_premium(user):
        return True
    channels = await list_active_channels(session)
    if not channels:
        return True
    for ch in channels:
        try:
            member = await bot.get_chat_member(ch.channel_id, user.tg_id)
            if member.status in {
                ChatMemberStatus.LEFT,
                ChatMemberStatus.KICKED,
            }:
                return False
        except Exception:
            return False
    return True
