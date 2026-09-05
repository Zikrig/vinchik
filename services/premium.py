from __future__ import annotations

from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

from aiogram import Bot
from aiogram.exceptions import TelegramAPIError
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from database.models import OrderStatus, PremiumOrder, PremiumPlan, User
from keyboards.inline import main_menu_kb
from locales import t
from services.users import premium_extension_base

PLAN_TITLE_MAX = 128
PLAN_PRICE_MAX = 64
PLAN_DAYS_MIN = 1
PLAN_DAYS_MAX = 365

_TZ = ZoneInfo("Asia/Dushanbe")


def _fmt_dt(dt: datetime | None) -> str:
    if dt is None:
        return "—"
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return dt.astimezone(_TZ).strftime("%d.%m.%Y %H:%M")


async def notify_premium_activated(bot: Bot, user: User) -> None:
    lang = user.language or "ru"
    text = t("premium_activated", lang, dt=_fmt_dt(user.premium_until))
    try:
        await bot.send_message(user.tg_id, text, reply_markup=main_menu_kb(lang))
    except TelegramAPIError:
        pass


async def list_active_plans(session: AsyncSession) -> list[PremiumPlan]:
    result = await session.execute(
        select(PremiumPlan).where(PremiumPlan.is_active.is_(True)).order_by(PremiumPlan.days)
    )
    return list(result.scalars().all())


async def list_plans(session: AsyncSession) -> list[PremiumPlan]:
    result = await session.execute(
        select(PremiumPlan).order_by(PremiumPlan.days, PremiumPlan.id)
    )
    return list(result.scalars().all())


async def get_plan(session: AsyncSession, plan_id: int) -> PremiumPlan | None:
    return await session.get(PremiumPlan, plan_id)


def serialize_plan(plan: PremiumPlan) -> dict:
    return {
        "id": plan.id,
        "title": plan.title,
        "days": plan.days,
        "price_text": plan.price_text,
        "is_active": bool(plan.is_active),
    }


def _clean_plan_fields(
    title: str, days: int | str, price_text: str
) -> tuple[str, int, str]:
    cleaned_title = title.strip()[:PLAN_TITLE_MAX]
    cleaned_price = price_text.strip()[:PLAN_PRICE_MAX]
    if not cleaned_title:
        raise ValueError("Нужно название тарифа.")
    if not cleaned_price:
        raise ValueError("Нужна сумма оплаты.")
    try:
        days_n = int(str(days).strip())
    except (TypeError, ValueError):
        raise ValueError("Срок — целое число дней.") from None
    if days_n < PLAN_DAYS_MIN or days_n > PLAN_DAYS_MAX:
        raise ValueError(f"Срок: {PLAN_DAYS_MIN}–{PLAN_DAYS_MAX} дней.")
    return cleaned_title, days_n, cleaned_price


async def create_plan(
    session: AsyncSession, *, title: str, days: int | str, price_text: str
) -> PremiumPlan:
    title, days_n, price_text = _clean_plan_fields(title, days, price_text)
    plan = PremiumPlan(
        title=title, days=days_n, price_text=price_text, is_active=True
    )
    session.add(plan)
    await session.commit()
    await session.refresh(plan)
    return plan


async def update_plan(
    session: AsyncSession,
    plan_id: int,
    *,
    title: str | None = None,
    days: int | str | None = None,
    price_text: str | None = None,
) -> PremiumPlan | None:
    plan = await session.get(PremiumPlan, plan_id)
    if plan is None:
        return None
    new_title = plan.title if title is None else title
    new_days = plan.days if days is None else days
    new_price = plan.price_text if price_text is None else price_text
    title, days_n, price_text = _clean_plan_fields(new_title, new_days, new_price)
    plan.title = title
    plan.days = days_n
    plan.price_text = price_text
    await session.commit()
    await session.refresh(plan)
    return plan


async def toggle_plan(session: AsyncSession, plan_id: int) -> PremiumPlan | None:
    plan = await session.get(PremiumPlan, plan_id)
    if plan is None:
        return None
    plan.is_active = not plan.is_active
    await session.commit()
    await session.refresh(plan)
    return plan


async def delete_plan(session: AsyncSession, plan_id: int) -> bool:
    plan = await session.get(PremiumPlan, plan_id)
    if plan is None:
        return False
    n = await session.scalar(
        select(func.count())
        .select_from(PremiumOrder)
        .where(PremiumOrder.plan_id == plan_id)
    )
    if n:
        raise ValueError("Нельзя удалить: по тарифу уже есть заявки. Выключи его.")
    await session.delete(plan)
    await session.commit()
    return True


async def create_order(session: AsyncSession, user_id: int, plan_id: int) -> PremiumOrder | None:
    plan = await session.get(PremiumPlan, plan_id)
    if plan is None or not plan.is_active:
        return None

    # One pending order per user, including concurrent button presses.
    user = (
        await session.execute(
            select(User).where(User.tg_id == user_id).with_for_update()
        )
    ).scalar_one_or_none()
    if user is None:
        return None

    result = await session.execute(
        select(PremiumOrder)
        .where(
            PremiumOrder.user_id == user_id,
            PremiumOrder.status == OrderStatus.pending,
        )
        .order_by(PremiumOrder.created_at.desc())
        .limit(1)
    )
    existing = result.scalar_one_or_none()
    if existing is not None:
        if existing.plan_id != plan_id:
            existing.plan_id = plan_id
            await session.commit()
            await session.refresh(existing)
        return existing

    order = PremiumOrder(user_id=user_id, plan_id=plan_id, status=OrderStatus.pending)
    session.add(order)
    await session.commit()
    await session.refresh(order)
    return order


async def attach_receipt(
    session: AsyncSession,
    order_id: int,
    user_id: int,
    file_id: str,
    kind: str,
) -> PremiumOrder | None:
    order = await session.get(PremiumOrder, order_id)
    if order is None or order.user_id != user_id or order.status != OrderStatus.pending:
        return None
    order.receipt_file_id = file_id
    order.receipt_kind = kind
    await session.commit()
    await session.refresh(order)
    return order


async def approve_order(
    session: AsyncSession, order_id: int, admin_id: int | None
) -> tuple[PremiumOrder, User] | None:
    """``admin_id=None`` marks an action from the web panel (no Telegram id)."""
    order_ref = await session.get(PremiumOrder, order_id)
    if order_ref is None:
        return None
    user = (
        await session.execute(
            select(User)
            .where(User.tg_id == order_ref.user_id)
            .with_for_update()
        )
    ).scalar_one_or_none()
    if user is None:
        return None
    order = (
        await session.execute(
            select(PremiumOrder)
            .where(PremiumOrder.id == order_id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
    ).scalar_one_or_none()
    if order is None or order.status != OrderStatus.pending:
        return None
    plan = await session.get(PremiumPlan, order.plan_id)
    if plan is None or user is None:
        return None
    now = datetime.now(UTC)
    base = premium_extension_base(user, now)
    user.premium_until = base + timedelta(days=plan.days)
    order.status = OrderStatus.approved
    order.processed_at = now
    order.processed_by = admin_id
    await session.commit()
    return order, user


async def reject_order(
    session: AsyncSession, order_id: int, admin_id: int | None
) -> PremiumOrder | None:
    order = (
        await session.execute(
            select(PremiumOrder)
            .where(PremiumOrder.id == order_id)
            .with_for_update()
        )
    ).scalar_one_or_none()
    if order is None or order.status != OrderStatus.pending:
        return None
    order.status = OrderStatus.rejected
    order.processed_at = datetime.now(UTC)
    order.processed_by = admin_id
    await session.commit()
    return order


async def grant_premium_days(session: AsyncSession, user_id: int, days: int) -> User | None:
    user = (
        await session.execute(
            select(User).where(User.tg_id == user_id).with_for_update()
        )
    ).scalar_one_or_none()
    if user is None:
        return None
    now = datetime.now(UTC)
    base = premium_extension_base(user, now)
    user.premium_until = base + timedelta(days=days)
    await session.commit()
    return user


async def revoke_premium(session: AsyncSession, user_id: int) -> User | None:
    user = (
        await session.execute(
            select(User).where(User.tg_id == user_id).with_for_update()
        )
    ).scalar_one_or_none()
    if user is None:
        return None
    user.premium_until = datetime(2004, 1, 1, tzinfo=UTC)
    await session.commit()
    return user


async def list_user_orders(
    session: AsyncSession, user_id: int, *, limit: int = 20
) -> list[tuple[PremiumOrder, PremiumPlan | None]]:
    result = await session.execute(
        select(PremiumOrder, PremiumPlan)
        .outerjoin(PremiumPlan, PremiumPlan.id == PremiumOrder.plan_id)
        .where(PremiumOrder.user_id == user_id)
        .order_by(PremiumOrder.created_at.desc())
        .limit(limit)
    )
    return [(order, plan) for order, plan in result.all()]


async def get_order_with_plan(
    session: AsyncSession, order_id: int, user_id: int
) -> tuple[PremiumOrder, PremiumPlan | None] | None:
    result = await session.execute(
        select(PremiumOrder, PremiumPlan)
        .outerjoin(PremiumPlan, PremiumPlan.id == PremiumOrder.plan_id)
        .where(PremiumOrder.id == order_id, PremiumOrder.user_id == user_id)
    )
    row = result.first()
    if row is None:
        return None
    return row[0], row[1]


async def list_pending_orders(session: AsyncSession) -> list[PremiumOrder]:
    result = await session.execute(
        select(PremiumOrder)
        .where(PremiumOrder.status == OrderStatus.pending)
        .order_by(PremiumOrder.created_at.desc())
    )
    return list(result.scalars().all())


async def list_premium_users(session: AsyncSession) -> list[User]:
    now = datetime.now(UTC)
    result = await session.execute(
        select(User)
        .where(User.premium_until.is_not(None), User.premium_until > now)
        .order_by(User.premium_until.asc())
    )
    return list(result.scalars().all())
