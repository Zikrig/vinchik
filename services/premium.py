from __future__ import annotations

from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

from aiogram import Bot
from aiogram.exceptions import TelegramAPIError
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from database.models import OrderStatus, PremiumOrder, PremiumPlan, User
from keyboards.inline import main_menu_kb
from locales import t

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


async def create_order(session: AsyncSession, user_id: int, plan_id: int) -> PremiumOrder:
    order = PremiumOrder(user_id=user_id, plan_id=plan_id, status=OrderStatus.pending)
    session.add(order)
    await session.commit()
    await session.refresh(order)
    return order


async def approve_order(
    session: AsyncSession, order_id: int, admin_id: int
) -> tuple[PremiumOrder, User] | None:
    order = await session.get(PremiumOrder, order_id)
    if order is None or order.status != OrderStatus.pending:
        return None
    plan = await session.get(PremiumPlan, order.plan_id)
    user = await session.get(User, order.user_id)
    if plan is None or user is None:
        return None
    now = datetime.now(UTC)
    base = user.premium_until if user.premium_until and user.premium_until > now else now
    if base.tzinfo is None:
        base = base.replace(tzinfo=UTC)
    user.premium_until = base + timedelta(days=plan.days)
    order.status = OrderStatus.approved
    order.processed_at = now
    order.processed_by = admin_id
    await session.commit()
    return order, user


async def reject_order(session: AsyncSession, order_id: int, admin_id: int) -> PremiumOrder | None:
    order = await session.get(PremiumOrder, order_id)
    if order is None or order.status != OrderStatus.pending:
        return None
    order.status = OrderStatus.rejected
    order.processed_at = datetime.now(UTC)
    order.processed_by = admin_id
    await session.commit()
    return order


async def grant_premium_days(session: AsyncSession, user_id: int, days: int) -> User | None:
    user = await session.get(User, user_id)
    if user is None:
        return None
    now = datetime.now(UTC)
    base = user.premium_until if user.premium_until and user.premium_until > now else now
    if base.tzinfo is None:
        base = base.replace(tzinfo=UTC)
    user.premium_until = base + timedelta(days=days)
    await session.commit()
    return user


async def revoke_premium(session: AsyncSession, user_id: int) -> User | None:
    user = await session.get(User, user_id)
    if user is None:
        return None
    user.premium_until = None
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
