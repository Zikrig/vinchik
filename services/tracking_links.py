from __future__ import annotations

import secrets
from datetime import UTC, date, datetime, timedelta
from zoneinfo import ZoneInfo

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from config import settings
from database.models import TrackingClick, TrackingLink

_TZ = ZoneInfo("Asia/Dushanbe")
_NAME_MAX = 128
_CODE_LEN = 8

PRESETS = (
    "today",
    "yesterday",
    "day_before",
    "last_3_days",
    "week",
    "month",
    "quarter",
    "year",
    "all",
)


def public_url(code: str) -> str:
    username = (settings.bot_username or "").lstrip("@").strip()
    if not username:
        return f"?start={code}"
    return f"https://t.me/{username}?start={code}"


def _new_code() -> str:
    return secrets.token_hex(_CODE_LEN // 2)


def _clean_name(name: str) -> str:
    cleaned = " ".join((name or "").strip().split())
    if not cleaned:
        raise ValueError("Название не может быть пустым.")
    if len(cleaned) > _NAME_MAX:
        raise ValueError(f"Название слишком длинное (макс. {_NAME_MAX}).")
    return cleaned


async def list_links(session: AsyncSession) -> list[TrackingLink]:
    result = await session.execute(
        select(TrackingLink).order_by(TrackingLink.id.desc())
    )
    return list(result.scalars().all())


async def get_link(session: AsyncSession, link_id: int) -> TrackingLink | None:
    return await session.get(TrackingLink, link_id)


async def get_link_by_code(session: AsyncSession, code: str) -> TrackingLink | None:
    code = (code or "").strip()
    if not code:
        return None
    result = await session.execute(
        select(TrackingLink).where(TrackingLink.code == code)
    )
    return result.scalar_one_or_none()


async def create_link(session: AsyncSession, name: str) -> TrackingLink:
    cleaned = _clean_name(name)
    for _ in range(12):
        code = _new_code()
        exists = await get_link_by_code(session, code)
        if exists is None:
            link = TrackingLink(code=code, name=cleaned)
            session.add(link)
            await session.commit()
            await session.refresh(link)
            return link
    raise RuntimeError("Не удалось сгенерировать уникальный код ссылки.")


async def rename_link(session: AsyncSession, link_id: int, name: str) -> TrackingLink | None:
    link = await get_link(session, link_id)
    if link is None:
        return None
    link.name = _clean_name(name)
    await session.commit()
    await session.refresh(link)
    return link


async def delete_link(session: AsyncSession, link_id: int) -> bool:
    link = await get_link(session, link_id)
    if link is None:
        return False
    await session.delete(link)
    await session.commit()
    return True


async def record_click(
    session: AsyncSession, code: str, user_id: int | None
) -> TrackingLink | None:
    """Count a deep-link open. Unknown codes are ignored."""
    link = await get_link_by_code(session, code)
    if link is None:
        return None
    session.add(TrackingClick(link_id=link.id, user_id=user_id))
    await session.commit()
    return link


def _day_bounds(d: date) -> tuple[datetime, datetime]:
    start = datetime(d.year, d.month, d.day, tzinfo=_TZ)
    end = start + timedelta(days=1)
    return start.astimezone(UTC), end.astimezone(UTC)


def resolve_range(
    *,
    preset: str | None = None,
    date_from: date | None = None,
    date_to: date | None = None,
    now: datetime | None = None,
) -> tuple[datetime | None, datetime | None, str]:
    """
    Inclusive calendar days in Asia/Dushanbe.
    Returns (utc_start, utc_end_exclusive, label). None/None = all time.
    """
    now_local = (now or datetime.now(UTC)).astimezone(_TZ)
    today = now_local.date()

    if date_from is not None or date_to is not None:
        a = date_from or date_to or today
        b = date_to or date_from or today
        if a > b:
            a, b = b, a
        start_utc, _ = _day_bounds(a)
        _, end_utc = _day_bounds(b)
        label = f"{a.isoformat()} — {b.isoformat()}"
        return start_utc, end_utc, label

    key = (preset or "all").strip().lower()
    if key not in PRESETS:
        key = "all"

    if key == "all":
        return None, None, "за всё время"
    if key == "today":
        start_utc, end_utc = _day_bounds(today)
        return start_utc, end_utc, "сегодня"
    if key == "yesterday":
        d = today - timedelta(days=1)
        start_utc, end_utc = _day_bounds(d)
        return start_utc, end_utc, "вчера"
    if key == "day_before":
        d = today - timedelta(days=2)
        start_utc, end_utc = _day_bounds(d)
        return start_utc, end_utc, "позавчера"
    if key == "last_3_days":
        a = today - timedelta(days=2)
        start_utc, _ = _day_bounds(a)
        _, end_utc = _day_bounds(today)
        return start_utc, end_utc, "за 3 дня"
    if key == "week":
        a = today - timedelta(days=6)
        start_utc, _ = _day_bounds(a)
        _, end_utc = _day_bounds(today)
        return start_utc, end_utc, "за неделю"
    if key == "month":
        a = today - timedelta(days=29)
        start_utc, _ = _day_bounds(a)
        _, end_utc = _day_bounds(today)
        return start_utc, end_utc, "за месяц"
    if key == "quarter":
        a = today - timedelta(days=89)
        start_utc, _ = _day_bounds(a)
        _, end_utc = _day_bounds(today)
        return start_utc, end_utc, "за 3 месяца"
    if key == "year":
        a = today - timedelta(days=364)
        start_utc, _ = _day_bounds(a)
        _, end_utc = _day_bounds(today)
        return start_utc, end_utc, "за год"
    return None, None, "за всё время"


async def click_counts(
    session: AsyncSession,
    *,
    start: datetime | None = None,
    end: datetime | None = None,
) -> list[tuple[TrackingLink, int]]:
    """All links with click counts in [start, end). Links with 0 still listed."""
    links = await list_links(session)
    if not links:
        return []

    stmt = (
        select(TrackingClick.link_id, func.count())
        .group_by(TrackingClick.link_id)
    )
    if start is not None:
        stmt = stmt.where(TrackingClick.created_at >= start)
    if end is not None:
        stmt = stmt.where(TrackingClick.created_at < end)
    result = await session.execute(stmt)
    by_id = {int(lid): int(n) for lid, n in result.all()}
    return [(link, by_id.get(link.id, 0)) for link in links]


def format_stats_message(rows: list[tuple[TrackingLink, int]], *, title: str) -> str:
    if not rows:
        return f"📊 {title}\n\nСсылок пока нет."
    width = max((len(str(n)) for _, n in rows), default=1)
    lines = [f"📊 {title}", ""]
    for link, n in rows:
        name = (link.name or "—").replace("\n", " ")
        if len(name) > 40:
            name = name[:37] + "…"
        lines.append(f"<code>{n:>{width}}</code>  {html_escape(name)}")
    total = sum(n for _, n in rows)
    lines.append("")
    lines.append(f"Всего переходов: <b>{total}</b>")
    return "\n".join(lines)


def html_escape(s: str) -> str:
    return (
        s.replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
    )
