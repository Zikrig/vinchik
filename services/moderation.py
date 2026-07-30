"""Silent moderation flags (suspicious). Never shown to end users."""

from __future__ import annotations

import asyncio
import logging
import re
from datetime import UTC, datetime, timedelta

from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from database.models import Like, LikeAction, Profile, Report, User
from database.session import async_session_maker

logger = logging.getLogger(__name__)

# Daily volume → suspicious (UTC calendar day).
DAILY_LIKE_SUSPICIOUS = 150
DAILY_MESSAGE_SUSPICIOUS = 150
# Distinct script/layout families in one message.
MAX_LAYOUTS_BEFORE_FLAG = 3

POLL_SECONDS = 120

_LAYOUTS: list[tuple[str, re.Pattern[str]]] = [
    ("latin", re.compile(r"[A-Za-z]")),
    ("cyrillic", re.compile(r"[\u0400-\u04FF]")),
    ("arabic", re.compile(r"[\u0600-\u06FF]")),
    ("greek", re.compile(r"[\u0370-\u03FF]")),
    ("hebrew", re.compile(r"[\u0590-\u05FF]")),
    ("cjk", re.compile(r"[\u4E00-\u9FFF\u3040-\u30FF\uAC00-\uD7AF]")),
    ("devanagari", re.compile(r"[\u0900-\u097F]")),
    ("thai", re.compile(r"[\u0E00-\u0E7F]")),
]


def detect_layouts(text: str) -> list[str]:
    found: list[str] = []
    for name, pattern in _LAYOUTS:
        if pattern.search(text or ""):
            found.append(name)
    return found


def layouts_over_limit(text: str) -> bool:
    return len(detect_layouts(text)) > MAX_LAYOUTS_BEFORE_FLAG


async def mark_suspicious(
    session: AsyncSession,
    user_id: int,
    reason: str,
    *,
    commit: bool = True,
) -> bool:
    """Set suspicious flag. Returns True if newly flagged or reason updated. Silent."""
    user = await session.get(User, user_id)
    if user is None or user.is_test or user_id <= 0:
        return False
    reason = (reason or "").strip()[:500]
    if not reason:
        return False
    now = datetime.now(UTC)
    changed = False
    if not user.is_suspicious:
        user.is_suspicious = True
        user.suspicious_at = now
        user.suspicious_reason = reason
        changed = True
    elif reason and reason not in (user.suspicious_reason or ""):
        prev = (user.suspicious_reason or "").strip()
        user.suspicious_reason = f"{prev}; {reason}"[:500] if prev else reason
        user.suspicious_at = now
        changed = True
    if changed and commit:
        await session.commit()
    if changed:
        logger.info("suspicious user=%s reason=%s", user_id, reason)
    return changed


async def clear_suspicious(session: AsyncSession, user_id: int) -> User | None:
    from services.users import load_user_with_profile

    user = await load_user_with_profile(session, user_id)
    if user is None:
        return None
    user.is_suspicious = False
    user.suspicious_reason = None
    user.suspicious_at = None
    await session.commit()
    return user


async def count_actions_today(
    session: AsyncSession, user_id: int, action: LikeAction
) -> int:
    start = datetime.now(UTC).replace(hour=0, minute=0, second=0, microsecond=0)
    result = await session.execute(
        select(func.count())
        .select_from(Like)
        .where(
            Like.from_user_id == user_id,
            Like.action == action,
            Like.created_at >= start,
        )
    )
    return int(result.scalar_one())


async def check_user_volume(
    session: AsyncSession, user_id: int, *, commit: bool = True
) -> None:
    likes = await count_actions_today(session, user_id, LikeAction.like)
    if likes > DAILY_LIKE_SUSPICIOUS:
        await mark_suspicious(
            session,
            user_id,
            f"более {DAILY_LIKE_SUSPICIOUS} лайков за сутки UTC ({likes})",
            commit=commit,
        )
        return
    msgs = await count_actions_today(session, user_id, LikeAction.message)
    if msgs > DAILY_MESSAGE_SUSPICIOUS:
        await mark_suspicious(
            session,
            user_id,
            f"более {DAILY_MESSAGE_SUSPICIOUS} сообщений за сутки UTC ({msgs})",
            commit=commit,
        )


async def check_message_layouts(
    session: AsyncSession, user_id: int, text: str, *, commit: bool = True
) -> None:
    layouts = detect_layouts(text)
    if len(layouts) > MAX_LAYOUTS_BEFORE_FLAG:
        await mark_suspicious(
            session,
            user_id,
            f"в сообщении более {MAX_LAYOUTS_BEFORE_FLAG} раскладок: {', '.join(layouts)}",
            commit=commit,
        )


async def on_like_recorded(
    session: AsyncSession,
    user_id: int,
    action: LikeAction,
    message_text: str | None = None,
    *,
    commit: bool = True,
) -> None:
    """Hook after a successful like/message — silent checks."""
    try:
        if action == LikeAction.message and message_text:
            await check_message_layouts(session, user_id, message_text, commit=commit)
        if action in (LikeAction.like, LikeAction.message):
            await check_user_volume(session, user_id, commit=commit)
    except Exception:
        logger.exception("moderation hook failed user=%s", user_id)


async def sweep_suspicious_candidates(session: AsyncSession) -> int:
    """Periodic: volume over threshold today; layout scan; reports ≥2."""
    from services.reports import REPORT_SUSPICIOUS_THRESHOLD, REPORT_WINDOW_DAYS

    start = datetime.now(UTC).replace(hour=0, minute=0, second=0, microsecond=0)
    flagged = 0

    for action, thr, label in (
        (LikeAction.like, DAILY_LIKE_SUSPICIOUS, "лайков"),
        (LikeAction.message, DAILY_MESSAGE_SUSPICIOUS, "сообщений"),
    ):
        result = await session.execute(
            select(Like.from_user_id, func.count())
            .where(Like.action == action, Like.created_at >= start)
            .group_by(Like.from_user_id)
            .having(func.count() > thr)
        )
        for uid, n in result.all():
            if await mark_suspicious(
                session,
                int(uid),
                f"более {thr} {label} за сутки UTC ({int(n)})",
                commit=False,
            ):
                flagged += 1

    since = datetime.now(UTC) - timedelta(hours=6)
    result = await session.execute(
        select(Like)
        .where(
            Like.action == LikeAction.message,
            Like.created_at >= since,
            or_(
                Like.message_text.is_not(None),
                Like.message_payload.is_not(None),
            ),
        )
        .order_by(Like.created_at.desc())
        .limit(500)
    )
    from services.likes import _like_payload, payload_text

    for like in result.scalars().all():
        text = (like.message_text or "").strip()
        if not text:
            text = payload_text(_like_payload(like)) or ""
        if layouts_over_limit(text):
            layouts = detect_layouts(text)
            if await mark_suspicious(
                session,
                like.from_user_id,
                f"в сообщении более {MAX_LAYOUTS_BEFORE_FLAG} раскладок: {', '.join(layouts)}",
                commit=False,
            ):
                flagged += 1

    reports_since = datetime.now(UTC) - timedelta(days=REPORT_WINDOW_DAYS)
    result = await session.execute(
        select(Report.to_user_id, func.count())
        .where(Report.created_at >= reports_since)
        .group_by(Report.to_user_id)
        .having(func.count() >= REPORT_SUSPICIOUS_THRESHOLD)
    )
    for uid, n in result.all():
        if await mark_suspicious(
            session,
            int(uid),
            f"жалоб за {REPORT_WINDOW_DAYS} дн.: {int(n)}",
            commit=False,
        ):
            flagged += 1
    if flagged:
        await session.commit()
    return flagged


async def list_suspicious_users(session: AsyncSession) -> list[dict]:
    from services.reports import REPORT_WINDOW_DAYS

    result = await session.execute(
        select(User, Profile)
        .outerjoin(Profile, Profile.user_id == User.tg_id)
        .where(User.is_suspicious.is_(True))
        .order_by(User.suspicious_at.desc().nulls_last())
    )
    users = result.all()
    user_ids = [user.tg_id for user, _ in users]
    report_counts: dict[int, int] = {}
    messages_by_user: dict[int, list[dict]] = {uid: [] for uid in user_ids}
    if user_ids:
        reports_since = datetime.now(UTC) - timedelta(days=REPORT_WINDOW_DAYS)
        report_result = await session.execute(
            select(Report.to_user_id, func.count())
            .where(
                Report.to_user_id.in_(user_ids),
                Report.created_at >= reports_since,
            )
            .group_by(Report.to_user_id)
        )
        report_counts = {int(uid): int(n) for uid, n in report_result.all()}

        ranked_messages = (
            select(
                Like.from_user_id.label("from_user_id"),
                Like.to_user_id.label("to_user_id"),
                Like.message_text.label("message_text"),
                Like.created_at.label("created_at"),
                func.row_number()
                .over(
                    partition_by=Like.from_user_id,
                    order_by=Like.created_at.desc(),
                )
                .label("row_n"),
            )
            .where(
                Like.from_user_id.in_(user_ids),
                Like.action == LikeAction.message,
                Like.message_text.is_not(None),
            )
            .subquery()
        )
        message_result = await session.execute(
            select(ranked_messages)
            .where(ranked_messages.c.row_n <= 20)
            .order_by(
                ranked_messages.c.from_user_id,
                ranked_messages.c.created_at.desc(),
            )
        )
        for row in message_result.mappings():
            text = (row["message_text"] or "").strip()
            if text:
                messages_by_user[int(row["from_user_id"])].append(
                    {
                        "to_user_id": row["to_user_id"],
                        "text": text,
                        "created_at": row["created_at"],
                        "layouts": detect_layouts(text),
                    }
                )

    out: list[dict] = []
    for user, profile in users:
        out.append(
            {
                "tg_id": user.tg_id,
                "username": user.username,
                "is_blocked": user.is_blocked,
                "suspicious_at": user.suspicious_at,
                "suspicious_reason": user.suspicious_reason or "",
                "reports_n": report_counts.get(user.tg_id, 0),
                "messages": messages_by_user.get(user.tg_id, []),
                "profile": None
                if profile is None
                else {
                    "photo_file_id": profile.photo_file_id,
                    "name": profile.name,
                    "age": profile.age,
                    "city_name": profile.city_name,
                    "gender": profile.gender.value if profile.gender else None,
                    "looking_for": profile.looking_for.value if profile.looking_for else None,
                    "description": profile.description,
                },
            }
        )
    return out


async def list_user_messages(
    session: AsyncSession, user_id: int, *, limit: int = 30
) -> list[dict]:
    result = await session.execute(
        select(Like)
        .where(
            Like.from_user_id == user_id,
            Like.action == LikeAction.message,
            Like.message_text.is_not(None),
        )
        .order_by(Like.created_at.desc())
        .limit(limit)
    )
    rows = []
    for like in result.scalars().all():
        text = (like.message_text or "").strip()
        if not text:
            continue
        rows.append(
            {
                "to_user_id": like.to_user_id,
                "text": text,
                "created_at": like.created_at,
                "layouts": detect_layouts(text),
            }
        )
    return rows


async def moderation_loop() -> None:
    await asyncio.sleep(20)
    while True:
        try:
            async with async_session_maker() as session:
                n = await sweep_suspicious_candidates(session)
                if n:
                    logger.info("moderation sweep flagged=%s", n)
        except Exception:
            logger.exception("moderation loop error")
        await asyncio.sleep(POLL_SECONDS)
