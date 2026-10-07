"""Premium subscription: first YooKassa payment saves the card, renewals charge it."""

from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta

from aiogram import Bot
from aiogram.exceptions import TelegramAPIError
from sqlalchemy import or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from database.models import OrderStatus, PremiumOrder, PremiumPlan, User
from database.session import async_session_maker
from keyboards.inline import main_menu_kb
from locales import t
from services.premium import approve_order, notify_premium_activated
from services.users import aware_utc
from services.yookassa_api import (
    YooKassaError,
    create_payment,
    get_payment,
    rub_amount,
    yookassa_configured,
)

logger = logging.getLogger(__name__)

RENEW_LOOKAHEAD = timedelta(minutes=30)
RENEW_RETRY = timedelta(days=1)
RENEW_GIVE_UP = timedelta(days=3)


def _meta_int(meta: dict, key: str) -> int | None:
    raw = str(meta.get(key) or "").strip()
    if not raw.isdigit():
        return None
    return int(raw)


async def bind_subscription(
    session: AsyncSession,
    user_id: int,
    plan_id: int | None,
    payment_method_id: str | None,
) -> User | None:
    user = await session.get(User, user_id)
    if user is None:
        return None
    if payment_method_id and plan_id is not None:
        user.yk_payment_method_id = payment_method_id[:64]
        user.yk_plan_id = plan_id
        user.yk_renew = True
        user.yk_renew_attempt = 0
        user.yk_retry_after = None
    await session.commit()
    await session.refresh(user)
    return user


async def cancel_subscription(session: AsyncSession, user_id: int) -> User | None:
    user = await session.get(User, user_id)
    if user is None:
        return None
    user.yk_renew = False
    user.yk_payment_method_id = None
    user.yk_plan_id = None
    user.yk_retry_after = None
    user.yk_renew_attempt = 0
    await session.commit()
    await session.refresh(user)
    return user


async def fulfill_payment(session: AsyncSession, payment: dict) -> tuple[User | None, bool]:
    """Grant premium for a succeeded payment. Second call for the same id is a no-op."""
    if payment.get("status") != "succeeded":
        return None, False
    pid = str(payment.get("id") or "")
    if not pid:
        return None, False
    meta = payment.get("metadata") or {}
    if not isinstance(meta, dict):
        meta = {}
    kind = str(meta.get("kind") or "initial")
    plan_id = _meta_int(meta, "plan_id")
    user_id = _meta_int(meta, "user_id")
    order_id = _meta_int(meta, "order_id")
    method = payment.get("payment_method") or {}
    method_id = None
    if isinstance(method, dict) and method.get("saved") and method.get("id"):
        method_id = str(method["id"])

    existing = await session.scalar(
        select(PremiumOrder).where(PremiumOrder.yk_payment_id == pid)
    )
    if existing is not None and existing.status == OrderStatus.approved:
        if method_id:
            await bind_subscription(session, existing.user_id, existing.plan_id, method_id)
        return await session.get(User, existing.user_id), False

    if kind == "renew":
        if user_id is None or plan_id is None:
            return None, False
        if existing is None:
            existing = PremiumOrder(
                user_id=user_id,
                plan_id=plan_id,
                status=OrderStatus.pending,
                yk_payment_id=pid,
            )
            session.add(existing)
            try:
                await session.commit()
            except IntegrityError:
                await session.rollback()
                existing = await session.scalar(
                    select(PremiumOrder).where(PremiumOrder.yk_payment_id == pid)
                )
        if existing is None:
            return None, False
        if existing.status == OrderStatus.approved:
            return await session.get(User, existing.user_id), False
        order_id = existing.id
    else:
        if order_id is None:
            return None, False
        order = await session.get(PremiumOrder, order_id)
        if order is None:
            return None, False
        if order.status == OrderStatus.approved:
            if method_id:
                await bind_subscription(session, order.user_id, order.plan_id, method_id)
            return await session.get(User, order.user_id), False
        order.yk_payment_id = pid
        await session.commit()

    if order_id is None:
        return None, False
    result = await approve_order(session, order_id, None)
    if result is None:
        owner_id = user_id
        if owner_id is None and existing is not None:
            owner_id = existing.user_id
        user = await session.get(User, owner_id) if owner_id else None
        return user, False
    order, user = result
    if method_id:
        user = await bind_subscription(session, user.tg_id, order.plan_id, method_id)
    else:
        await session.refresh(user)
    return user, True


async def confirm_payment_id(session: AsyncSession, payment_id: str) -> tuple[User | None, bool]:
    payment = await get_payment(payment_id)
    return await fulfill_payment(session, payment)


async def poll_pending_payments(bot: Bot) -> None:
    """Ask YooKassa about unpaid orders — no HTTP notifications needed."""
    if not yookassa_configured():
        return
    async with async_session_maker() as session:
        result = await session.execute(
            select(PremiumOrder.id, PremiumOrder.yk_payment_id)
            .where(
                PremiumOrder.status == OrderStatus.pending,
                PremiumOrder.yk_payment_id.is_not(None),
            )
            .order_by(PremiumOrder.created_at.desc())
            .limit(40)
        )
        pending = [(int(oid), str(pid)) for oid, pid in result.all() if pid]

    for _order_id, payment_id in pending:
        try:
            async with async_session_maker() as session:
                user, activated = await confirm_payment_id(session, payment_id)
            if activated and user is not None:
                await notify_premium_activated(bot, user)
        except YooKassaError:
            logger.warning("yookassa poll failed payment_id=%s", payment_id)
        except Exception:
            logger.exception("yookassa poll payment_id=%s", payment_id)


async def _notify_renew_failed(bot: Bot, user: User) -> None:
    lang = user.language or "ru"
    try:
        await bot.send_message(
            user.tg_id,
            t("premium_renew_failed", lang),
            reply_markup=main_menu_kb(lang),
        )
    except TelegramAPIError:
        pass


async def renew_due_subscriptions(bot: Bot) -> None:
    if not yookassa_configured():
        return
    now = datetime.now(UTC)
    async with async_session_maker() as session:
        due_at = now + RENEW_LOOKAHEAD
        result = await session.execute(
            select(User.tg_id)
            .where(
                User.yk_renew.is_(True),
                User.yk_payment_method_id.is_not(None),
                User.yk_plan_id.is_not(None),
                User.is_test.is_(False),
                User.tg_id > 0,
                or_(User.premium_until.is_(None), User.premium_until <= due_at),
                or_(User.yk_retry_after.is_(None), User.yk_retry_after <= now),
            )
            .limit(20)
        )
        ids = [int(x) for x in result.scalars().all()]

    for tg_id in ids:
        try:
            await _renew_one(bot, tg_id)
        except Exception:
            logger.exception("subscription renew failed tg_id=%s", tg_id)


async def _renew_one(bot: Bot, tg_id: int) -> None:
    async with async_session_maker() as session:
        user = await session.get(User, tg_id)
        if user is None or not user.yk_renew or not user.yk_payment_method_id or not user.yk_plan_id:
            return
        plan = await session.get(PremiumPlan, user.yk_plan_id)
        if plan is None or not plan.is_active:
            user.yk_renew = False
            await session.commit()
            await _notify_renew_failed(bot, user)
            return
        amount = rub_amount(plan.price_text)
        if amount is None:
            user.yk_renew = False
            await session.commit()
            await _notify_renew_failed(bot, user)
            return
        until = aware_utc(user.premium_until)
        until_ts = int(until.timestamp()) if until is not None else 0
        attempt = int(user.yk_renew_attempt or 0)
        method_id = user.yk_payment_method_id
        plan_id = plan.id
        title = plan.title

    try:
        payment = await create_payment(
            amount=amount,
            description=f"Подписка {title}",
            metadata={
                "user_id": str(tg_id),
                "plan_id": str(plan_id),
                "kind": "renew",
            },
            idempotence_key=f"renew-{tg_id}-{until_ts}-{attempt}",
            payment_method_id=method_id,
        )
    except YooKassaError:
        logger.exception("yookassa renew create tg_id=%s", tg_id)
        async with async_session_maker() as session:
            user = await session.get(User, tg_id)
            if user is not None:
                await _mark_renew_failed(session, user)
                await _notify_renew_failed(bot, user)
        return

    status = str(payment.get("status") or "")
    if status == "succeeded":
        async with async_session_maker() as session:
            user, activated = await fulfill_payment(session, payment)
        if activated and user is not None:
            await notify_premium_activated(bot, user)
        return

    async with async_session_maker() as session:
        user = await session.get(User, tg_id)
        if user is None:
            return
        if status == "canceled":
            await _mark_renew_failed(session, user)
            await _notify_renew_failed(bot, user)
            return
        user.yk_retry_after = datetime.now(UTC) + timedelta(hours=1)
        await session.commit()


async def _mark_renew_failed(session: AsyncSession, user: User) -> None:
    now = datetime.now(UTC)
    until = aware_utc(user.premium_until)
    user.yk_renew_attempt = int(user.yk_renew_attempt or 0) + 1
    user.yk_retry_after = now + RENEW_RETRY
    if until is not None and until < now - RENEW_GIVE_UP:
        user.yk_renew = False
        user.yk_payment_method_id = None
    await session.commit()
