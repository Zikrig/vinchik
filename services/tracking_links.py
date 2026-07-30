from __future__ import annotations

import re
from datetime import UTC, date, datetime, timedelta
from zoneinfo import ZoneInfo

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from config import settings
from database.models import TrackingClick, TrackingLink

_TZ = ZoneInfo("Asia/Dushanbe")
_NAME_MAX = 128
_CODE_MAX = 64  # Telegram start-параметр

# RU + TJ → латиница для deep-link кода.
_TRANSLIT = {
    "а": "a", "б": "b", "в": "v", "г": "g", "д": "d", "е": "e", "ё": "e",
    "ж": "zh", "з": "z", "и": "i", "й": "y", "к": "k", "л": "l", "м": "m",
    "н": "n", "о": "o", "п": "p", "р": "r", "с": "s", "т": "t", "у": "u",
    "ф": "f", "х": "h", "ц": "ts", "ч": "ch", "ш": "sh", "щ": "sch",
    "ъ": "", "ы": "y", "ь": "", "э": "e", "ю": "yu", "я": "ya",
    "ғ": "gh", "ӣ": "i", "қ": "q", "ӯ": "u", "ҳ": "h", "ҷ": "j",
}

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


def _clean_name(name: str) -> str:
    cleaned = " ".join((name or "").strip().split())
    if not cleaned:
        raise ValueError("Название не может быть пустым.")
    if len(cleaned) > _NAME_MAX:
        raise ValueError(f"Название слишком длинное (макс. {_NAME_MAX}).")
    return cleaned


def slugify_code(text: str) -> str:
    """Латиница/цифры/_/- для ?start=… из названия или явного кода."""
    out: list[str] = []
    for ch in (text or "").strip().lower():
        if ch in _TRANSLIT:
            out.append(_TRANSLIT[ch])
        elif "a" <= ch <= "z" or "0" <= ch <= "9":
            out.append(ch)
        elif ch in (" ", "-", "_", ".", "/", "\\", ","):
            out.append("_")
        # прочее отбрасываем
    code = re.sub(r"_+", "_", "".join(out)).strip("_-")
    return code[:_CODE_MAX]


def _clean_code(code: str | None, *, fallback_from: str) -> str:
    raw = (code or "").strip()
    if raw:
        cleaned = slugify_code(raw)
        # Если юзер уже дал почти-валидный код — сохраняем регистр через slugify (lower).
        if not cleaned:
            raise ValueError(
                "Код пустой после очистки. Используй латиницу, цифры, _ или -."
            )
    else:
        cleaned = slugify_code(fallback_from)
        if not cleaned:
            raise ValueError(
                "Не удалось сделать код из названия. Укажи код латиницей явно."
            )
    if len(cleaned) > _CODE_MAX:
        raise ValueError(f"Код слишком длинный (макс. {_CODE_MAX}).")
    if not re.fullmatch(r"[a-z0-9][a-z0-9_-]*", cleaned):
        raise ValueError("Код: латиница, цифры, _ и -; начинаться с буквы или цифры.")
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


async def _unique_code(session: AsyncSession, base: str) -> str:
    candidate = base[:_CODE_MAX]
    if await get_link_by_code(session, candidate) is None:
        return candidate
    for n in range(2, 100):
        suffix = f"_{n}"
        candidate = f"{base[: _CODE_MAX - len(suffix)]}{suffix}"
        if await get_link_by_code(session, candidate) is None:
            return candidate
    raise RuntimeError("Не удалось подобрать уникальный код ссылки.")


async def create_link(
    session: AsyncSession, name: str, code: str | None = None
) -> TrackingLink:
    cleaned = _clean_name(name)
    base = _clean_code(code, fallback_from=cleaned)
    # Явный код — строго уникальный; авто — с суффиксом _2, _3…
    if (code or "").strip():
        if await get_link_by_code(session, base) is not None:
            raise ValueError(f"Код «{base}» уже занят.")
        final = base
    else:
        final = await _unique_code(session, base)
    link = TrackingLink(code=final, name=cleaned)
    session.add(link)
    await session.commit()
    await session.refresh(link)
    return link


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
