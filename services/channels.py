"""Required advertising channels: CRUD helpers + subscription checks."""

from __future__ import annotations

import re
from dataclasses import dataclass

from aiogram import Bot
from aiogram.enums import ChatMemberStatus, ChatType
from aiogram.exceptions import TelegramAPIError
from aiogram.types import Chat, Message
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from database.models import RequiredChannel, User
from services.users import is_premium

_TME = re.compile(
    r"(?:https?://)?(?:t\.me|telegram\.me)/(?:s/)?(@?[\w]+|\+[\w-]+|joinchat/[\w-]+)",
    re.IGNORECASE,
)
_USERNAME = re.compile(r"^@?[A-Za-z][\w]{3,31}$")
_NUMERIC_ID = re.compile(r"^-?\d{5,}$")


class ChannelResolveError(Exception):
    """User-facing reason why channel could not be added."""


@dataclass
class ResolvedChannel:
    channel_id: str
    title: str
    invite_link: str


def parse_channel_ref(raw: str) -> str | None:
    """
    Normalize admin/web input to a Bot API chat ref.
    Accepts @nick, nick, t.me/nick, numeric -100… ids.
    Invite-only links (+… / joinchat) return None — need forward or numeric id.
    """
    text = (raw or "").strip()
    if not text:
        return None
    if _NUMERIC_ID.match(text):
        return text

    m = _TME.search(text.replace(" ", ""))
    if m:
        path = m.group(1)
        low = path.lower()
        if low.startswith("+") or low.startswith("joinchat/"):
            return None
        path = path.lstrip("@")
        if _USERNAME.match(path) or _USERNAME.match("@" + path):
            return f"@{path.lstrip('@')}"
        return None

    if text.startswith("@"):
        nick = text[1:]
        if _USERNAME.match("@" + nick):
            return f"@{nick}"
        return None

    if _USERNAME.match(text) or _USERNAME.match("@" + text):
        return f"@{text.lstrip('@')}"
    return None


def channel_button_url(ch: RequiredChannel) -> str | None:
    if ch.invite_link:
        return ch.invite_link
    cid = str(ch.channel_id or "")
    if cid.startswith("@"):
        return f"https://t.me/{cid.lstrip('@')}"
    if cid and not cid.startswith("-"):
        return f"https://t.me/{cid}"
    return None


def format_channels_lines(channels: list[RequiredChannel]) -> str:
    lines: list[str] = []
    for ch in channels:
        title = (ch.title or ch.channel_id or "канал").strip()
        url = channel_button_url(ch)
        if url:
            lines.append(f"• {title}\n  {url}")
        else:
            lines.append(f"• {title}")
    return "\n".join(lines)


async def list_active_channels(session: AsyncSession) -> list[RequiredChannel]:
    result = await session.execute(
        select(RequiredChannel).where(RequiredChannel.is_active.is_(True)).order_by(RequiredChannel.id)
    )
    return list(result.scalars().all())


async def list_all_channels(session: AsyncSession) -> list[RequiredChannel]:
    result = await session.execute(select(RequiredChannel).order_by(RequiredChannel.id))
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


async def _assert_bot_admin(bot: Bot, chat_id: int | str) -> None:
    me = await bot.get_me()
    try:
        member = await bot.get_chat_member(chat_id, me.id)
    except TelegramAPIError as exc:
        raise ChannelResolveError(
            "Бот не видит канал. Сделай бота администратором канала и попробуй снова."
        ) from exc
    if member.status not in {
        ChatMemberStatus.ADMINISTRATOR,
        ChatMemberStatus.CREATOR,
    }:
        raise ChannelResolveError(
            "Бот должен быть администратором канала (иначе нельзя проверить подписку)."
        )


def _from_chat(chat: Chat, invite_hint: str = "") -> ResolvedChannel:
    if chat.type not in {ChatType.CHANNEL, ChatType.SUPERGROUP}:
        raise ChannelResolveError("Нужен канал (или супергруппа), не личный чат.")
    if chat.username:
        channel_id = f"@{chat.username}"
        invite = invite_hint or f"https://t.me/{chat.username}"
    else:
        channel_id = str(chat.id)
        invite = invite_hint
    title = (chat.title or channel_id).strip()
    return ResolvedChannel(channel_id=channel_id, title=title, invite_link=invite)


async def resolve_channel_ref(bot: Bot, raw: str) -> ResolvedChannel:
    ref = parse_channel_ref(raw)
    if ref is None:
        text = (raw or "").strip()
        if _TME.search(text.replace(" ", "")):
            raise ChannelResolveError(
                "Приватную ссылку-приглашение нельзя разобрать. "
                "Пришли @ник, публичную ссылку t.me/ник, числовой id или перешли сообщение из канала."
            )
        raise ChannelResolveError(
            "Не понял канал. Пришли @ник, ссылку t.me/… или перешли сообщение из канала."
        )
    try:
        chat = await bot.get_chat(ref)
    except TelegramAPIError as exc:
        raise ChannelResolveError(
            "Не удалось открыть канал. Проверь @ник/ссылку и что бот — админ канала."
        ) from exc
    await _assert_bot_admin(bot, chat.id)
    invite_hint = ""
    if isinstance(ref, str) and ref.startswith("@"):
        invite_hint = f"https://t.me/{ref.lstrip('@')}"
    return _from_chat(chat, invite_hint)


async def resolve_channel_forward(bot: Bot, message: Message) -> ResolvedChannel:
    chat = message.forward_from_chat
    if chat is None and message.forward_origin is not None:
        origin = message.forward_origin
        chat_id = getattr(origin, "chat", None)
        if chat_id is not None:
            chat = chat_id
    if chat is None:
        raise ChannelResolveError(
            "Перешли сообщение из канала (не из лички). "
            "Сначала сделай бота администратором этого канала."
        )
    try:
        full = await bot.get_chat(chat.id)
    except TelegramAPIError as exc:
        raise ChannelResolveError(
            "Бот не видит этот канал. Добавь бота админом и перешли сообщение снова."
        ) from exc
    await _assert_bot_admin(bot, full.id)
    return _from_chat(full)


async def find_channel_by_ref(
    session: AsyncSession, channel_id: str
) -> RequiredChannel | None:
    result = await session.execute(
        select(RequiredChannel).where(RequiredChannel.channel_id == channel_id)
    )
    return result.scalar_one_or_none()


async def add_resolved_channel(
    session: AsyncSession,
    resolved: ResolvedChannel,
    *,
    title_override: str = "",
    invite_override: str = "",
) -> tuple[RequiredChannel, bool]:
    """
    Insert or reactivate channel. Returns (row, created_new).
    """
    existing = await find_channel_by_ref(session, resolved.channel_id)
    title = (title_override or resolved.title or resolved.channel_id).strip()
    invite = (invite_override or resolved.invite_link or "").strip()
    if existing:
        existing.title = title or existing.title
        if invite:
            existing.invite_link = invite
        existing.is_active = True
        await session.commit()
        await session.refresh(existing)
        return existing, False
    ch = RequiredChannel(
        channel_id=resolved.channel_id,
        title=title,
        invite_link=invite,
        is_active=True,
    )
    session.add(ch)
    await session.commit()
    await session.refresh(ch)
    return ch, True


async def toggle_channel(session: AsyncSession, channel_pk: int) -> RequiredChannel | None:
    ch = await session.get(RequiredChannel, channel_pk)
    if ch is None:
        return None
    ch.is_active = not ch.is_active
    await session.commit()
    await session.refresh(ch)
    return ch


async def delete_channel(session: AsyncSession, channel_pk: int) -> bool:
    ch = await session.get(RequiredChannel, channel_pk)
    if ch is None:
        return False
    await session.delete(ch)
    await session.commit()
    return True
