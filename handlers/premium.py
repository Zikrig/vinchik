from __future__ import annotations

from datetime import UTC, datetime
from zoneinfo import ZoneInfo

from aiogram import Bot, F, Router
from aiogram.exceptions import TelegramAPIError
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup
from sqlalchemy.ext.asyncio import AsyncSession

from config import settings
from database.models import OrderStatus, PremiumOrder, PremiumPlan
from handlers.common import load_user, show_main_menu
from keyboards.inline import main_menu_kb
from locales import t
from services.premium import (
    create_order,
    get_order_with_plan,
    list_active_plans,
    list_user_orders,
)
from services.settings_service import get_payment_info
from services.users import is_premium

router = Router()

# Tajikistan local time for display
_TZ = ZoneInfo("Asia/Dushanbe")


def _fmt_dt(dt: datetime | None) -> str:
    if dt is None:
        return "—"
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return dt.astimezone(_TZ).strftime("%d.%m.%Y %H:%M")


def _status_emoji(status: OrderStatus) -> str:
    if status == OrderStatus.approved:
        return "✅"
    if status == OrderStatus.rejected:
        return "❌"
    return "⏳"


def _status_label(status: OrderStatus, lang: str) -> str:
    key = f"premium_order_status_{status.value}"
    return t(key, lang)


def _premium_pay_kb(lang: str, order_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text=t("btn_i_paid", lang),
                    callback_data=f"prem:paid:{order_id}",
                )
            ],
            [InlineKeyboardButton(text=t("back", lang), callback_data="prem:menu")],
        ]
    )


def _premium_menu_kb(lang: str, plans: list[PremiumPlan]) -> InlineKeyboardMarkup:
    rows = [
        [
            InlineKeyboardButton(
                text=f"💎 {p.title} — {p.price_text}",
                callback_data=f"prem:buy:{p.id}",
            )
        ]
        for p in plans
    ]
    rows.append(
        [
            InlineKeyboardButton(
                text=t("btn_premium_history", lang),
                callback_data="prem:history",
            )
        ]
    )
    rows.append([InlineKeyboardButton(text=t("back", lang), callback_data="prem:back")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


async def _send_premium_menu(message, session: AsyncSession, user) -> None:
    lang = user.language
    if is_premium(user) and user.premium_until:
        status = t("premium_active_until", lang, dt=_fmt_dt(user.premium_until))
    else:
        status = t("premium_inactive", lang)
    plans = await list_active_plans(session)
    body = f"{status}\n\n{t('premium_choose', lang) if plans else t('premium_title', lang)}"
    await message.answer(body, reply_markup=_premium_menu_kb(lang, plans))


@router.callback_query(F.data == "menu:premium")
async def premium_menu(callback: CallbackQuery, session: AsyncSession) -> None:
    user = await load_user(session, callback.from_user.id)
    assert user and callback.message
    await callback.answer()
    await _send_premium_menu(callback.message, session, user)


@router.callback_query(F.data == "prem:menu")
async def premium_menu_again(callback: CallbackQuery, session: AsyncSession) -> None:
    user = await load_user(session, callback.from_user.id)
    assert user and callback.message
    await callback.answer()
    try:
        await callback.message.edit_reply_markup(reply_markup=None)
    except Exception:
        pass
    await _send_premium_menu(callback.message, session, user)


@router.callback_query(F.data == "prem:history")
async def premium_history(callback: CallbackQuery, session: AsyncSession) -> None:
    user = await load_user(session, callback.from_user.id)
    assert user and callback.message
    lang = user.language
    await callback.answer()
    try:
        await callback.message.edit_reply_markup(reply_markup=None)
    except Exception:
        pass
    rows_data = await list_user_orders(session, user.tg_id, limit=20)
    kb_rows: list[list[InlineKeyboardButton]] = []
    for order, plan in rows_data:
        plan_title = plan.title if plan else "—"
        label = f"{_status_emoji(order.status)} {_fmt_dt(order.created_at)} · {plan_title}"
        if len(label) > 64:
            label = label[:61] + "…"
        kb_rows.append(
            [
                InlineKeyboardButton(
                    text=label,
                    callback_data=f"prem:ord:{order.id}",
                )
            ]
        )
    kb_rows.append(
        [InlineKeyboardButton(text=t("back", lang), callback_data="prem:menu")]
    )
    text = (
        t("premium_history_title", lang)
        if rows_data
        else t("premium_history_empty", lang)
    )
    await callback.message.answer(
        text, reply_markup=InlineKeyboardMarkup(inline_keyboard=kb_rows)
    )


@router.callback_query(F.data.startswith("prem:ord:"))
async def premium_order_detail(callback: CallbackQuery, session: AsyncSession) -> None:
    user = await load_user(session, callback.from_user.id)
    assert user and callback.data and callback.message
    order_id = int(callback.data.split(":")[2])
    row = await get_order_with_plan(session, order_id, user.tg_id)
    if row is None:
        await callback.answer("—", show_alert=True)
        return
    order, plan = row
    lang = user.language
    processed_line = ""
    if order.processed_at:
        processed_line = t(
            "premium_order_processed", lang, processed=_fmt_dt(order.processed_at)
        )
    await callback.answer()
    await callback.message.answer(
        t(
            "premium_order_detail",
            lang,
            order_id=order.id,
            plan=plan.title if plan else "—",
            status=f"{_status_emoji(order.status)} {_status_label(order.status, lang)}",
            created=_fmt_dt(order.created_at),
            processed_line=processed_line,
        ).rstrip(),
        reply_markup=InlineKeyboardMarkup(
            inline_keyboard=[
                [InlineKeyboardButton(text=t("back", lang), callback_data="prem:history")]
            ]
        ),
    )


@router.callback_query(F.data.startswith("prem:buy:"))
async def premium_buy(callback: CallbackQuery, session: AsyncSession) -> None:
    user = await load_user(session, callback.from_user.id)
    assert user and callback.data and callback.message
    plan_id = int(callback.data.split(":")[2])
    order = await create_order(session, user.tg_id, plan_id)
    pay = await get_payment_info(session)
    await callback.answer()
    try:
        await callback.message.edit_reply_markup(reply_markup=None)
    except Exception:
        pass
    await callback.message.answer(
        t(
            "premium_pay",
            user.language,
            order_id=order.id,
            manager=pay["manager"],
            card=pay["card"],
            check_time=pay["check_time"],
        ),
        reply_markup=_premium_pay_kb(user.language, order.id),
    )


@router.callback_query(F.data.startswith("prem:paid:"))
async def premium_paid(callback: CallbackQuery, session: AsyncSession, bot: Bot) -> None:
    user = await load_user(session, callback.from_user.id)
    assert user and callback.data and callback.message
    order_id = int(callback.data.split(":")[2])
    await callback.answer()
    try:
        await callback.message.edit_reply_markup(reply_markup=None)
    except Exception:
        pass
    await callback.message.answer(
        t("premium_paid_thanks", user.language, order_id=order_id),
        reply_markup=main_menu_kb(user.language),
    )
    uname = f"@{user.username}" if user.username else "—"
    admin_text = (
        f"💳 Пользователь отметил оплату\n"
        f"Заявка #{order_id}\n"
        f"user: {user.tg_id} {uname}"
    )
    for aid in settings.admin_id_set:
        try:
            await bot.send_message(aid, admin_text)
        except TelegramAPIError:
            pass


@router.callback_query(F.data == "prem:back")
async def premium_back(callback: CallbackQuery, session: AsyncSession) -> None:
    user = await load_user(session, callback.from_user.id)
    assert user and callback.message
    await callback.answer()
    try:
        await callback.message.edit_reply_markup(reply_markup=None)
    except Exception:
        pass
    await show_main_menu(callback.message, user)
