from __future__ import annotations

import html
from datetime import UTC, datetime
from zoneinfo import ZoneInfo

from aiogram import Bot, F, Router
from aiogram.exceptions import TelegramAPIError
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message
from sqlalchemy.ext.asyncio import AsyncSession

from config import settings
from database.models import OrderStatus, PremiumOrder, PremiumPlan
from handlers.common import callback_context, message_user, show_main_menu
from keyboards.inline import main_menu_kb
from locales import t
from services.premium import (
    attach_receipt,
    create_order,
    get_order_with_plan,
    list_active_plans,
    list_user_orders,
)
from services.settings_service import get_payment_info
from services.users import is_premium
from states.premium import PremiumStates

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
                    text=t("btn_send_receipt", lang),
                    callback_data=f"prem:receipt:{order_id}",
                )
            ],
            [InlineKeyboardButton(text=t("back", lang), callback_data="prem:menu")],
        ]
    )


def _receipt_prompt_kb(lang: str, order_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text=t("btn_cancel", lang),
                    callback_data=f"prem:receipt_cancel:{order_id}",
                )
            ]
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
    choose = t("premium_choose", lang) if plans else t("premium_title", lang)
    body = f"{t('premium_benefits', lang)}\n\n{status}\n\n{choose}"
    await message.answer(body, reply_markup=_premium_menu_kb(lang, plans))


async def _notify_admins_receipt(
    bot: Bot, user, order: PremiumOrder, kind: str, file_id: str
) -> None:
    uname = f"@{user.username}" if user.username else "—"
    admin_text = (
        f"💳 Чек по оплате\n"
        f"Заявка #{order.id}\n"
        f"user: {user.tg_id} {uname}"
    )
    admin_kb = InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="✅ Подтвердить", callback_data=f"adm:rok:{order.id}"
                ),
                InlineKeyboardButton(
                    text="❌ Отклонить", callback_data=f"adm:rno:{order.id}"
                ),
            ]
        ]
    )
    for aid in settings.admin_id_set:
        try:
            if kind == "photo":
                await bot.send_photo(
                    aid, photo=file_id, caption=admin_text, reply_markup=admin_kb
                )
            else:
                await bot.send_document(
                    aid, document=file_id, caption=admin_text, reply_markup=admin_kb
                )
        except TelegramAPIError:
            try:
                await bot.send_message(aid, admin_text, reply_markup=admin_kb)
            except TelegramAPIError:
                pass


@router.callback_query(F.data == "menu:premium")
async def premium_menu(callback: CallbackQuery, session: AsyncSession) -> None:
    ctx = await callback_context(callback, session)
    if ctx is None:
        return
    user, message = ctx
    await callback.answer()
    await _send_premium_menu(message, session, user)


@router.callback_query(F.data == "prem:menu")
async def premium_menu_again(
    callback: CallbackQuery, session: AsyncSession, state: FSMContext
) -> None:
    ctx = await callback_context(callback, session)
    if ctx is None:
        return
    user, message = ctx
    await state.clear()
    await callback.answer()
    try:
        await message.edit_reply_markup(reply_markup=None)
    except Exception:
        pass
    await _send_premium_menu(message, session, user)


@router.callback_query(F.data == "prem:history")
async def premium_history(callback: CallbackQuery, session: AsyncSession) -> None:
    ctx = await callback_context(callback, session)
    if ctx is None:
        return
    user, message = ctx
    lang = user.language
    await callback.answer()
    try:
        await message.edit_reply_markup(reply_markup=None)
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
    await message.answer(
        text, reply_markup=InlineKeyboardMarkup(inline_keyboard=kb_rows)
    )


@router.callback_query(F.data.startswith("prem:ord:"))
async def premium_order_detail(callback: CallbackQuery, session: AsyncSession) -> None:
    ctx = await callback_context(callback, session)
    if ctx is None:
        return
    user, message = ctx
    order_id = int((callback.data or "").split(":")[2])
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
    await message.answer(
        t(
            "premium_order_detail",
            lang,
            order_id=order.id,
            plan=html.escape(plan.title) if plan else "—",
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
    ctx = await callback_context(callback, session)
    if ctx is None:
        return
    user, message = ctx
    plan_id = int((callback.data or "").split(":")[2])
    order = await create_order(session, user.tg_id, plan_id)
    if order is None:
        await callback.answer("—", show_alert=True)
        return
    pay = await get_payment_info(session)
    plan = await session.get(PremiumPlan, order.plan_id)
    await callback.answer()
    try:
        await message.edit_reply_markup(reply_markup=None)
    except Exception:
        pass
    await message.answer(
        t(
            "premium_pay",
            user.language,
            order_id=order.id,
            plan=html.escape(plan.title) if plan else "—",
            amount=html.escape(plan.price_text) if plan else "—",
            manager=html.escape(pay["manager"]),
            card=html.escape(pay["card"]),
            check_time=html.escape(pay["check_time"]),
        ),
        reply_markup=_premium_pay_kb(user.language, order.id),
    )


@router.callback_query(F.data.startswith("prem:receipt:"))
async def premium_ask_receipt(
    callback: CallbackQuery, session: AsyncSession, state: FSMContext
) -> None:
    ctx = await callback_context(callback, session)
    if ctx is None:
        return
    user, message = ctx
    order_id = int((callback.data or "").split(":")[2])
    row = await get_order_with_plan(session, order_id, user.tg_id)
    if row is None or row[0].status != OrderStatus.pending:
        await callback.answer("—", show_alert=True)
        return
    await callback.answer()
    try:
        await message.edit_reply_markup(reply_markup=None)
    except Exception:
        pass
    prompt = await message.answer(
        t("premium_send_receipt", user.language, order_id=order_id),
        reply_markup=_receipt_prompt_kb(user.language, order_id),
    )
    await state.set_state(PremiumStates.awaiting_receipt)
    await state.update_data(order_id=order_id, prompt_message_id=prompt.message_id)


@router.callback_query(
    PremiumStates.awaiting_receipt, F.data.startswith("prem:receipt_cancel:")
)
async def premium_receipt_cancel(
    callback: CallbackQuery, session: AsyncSession, state: FSMContext
) -> None:
    ctx = await callback_context(callback, session)
    if ctx is None:
        return
    user, message = ctx
    order_id = int((callback.data or "").split(":")[2])
    data = await state.get_data()
    if data.get("order_id") != order_id:
        await callback.answer("—", show_alert=True)
        return
    await state.clear()
    await callback.answer()
    try:
        await message.edit_reply_markup(reply_markup=None)
    except Exception:
        pass
    await message.answer(
        t("premium_receipt_cancelled", user.language),
        reply_markup=_premium_pay_kb(user.language, order_id),
    )


@router.message(PremiumStates.awaiting_receipt, F.photo)
async def premium_receipt_photo(
    message: Message, session: AsyncSession, state: FSMContext, bot: Bot
) -> None:
    await _accept_receipt(
        message, session, state, bot, kind="photo", file_id=message.photo[-1].file_id
    )


@router.message(PremiumStates.awaiting_receipt, F.document)
async def premium_receipt_document(
    message: Message, session: AsyncSession, state: FSMContext, bot: Bot
) -> None:
    if message.document is None:
        return
    await _accept_receipt(
        message,
        session,
        state,
        bot,
        kind="document",
        file_id=message.document.file_id,
    )


@router.message(PremiumStates.awaiting_receipt)
async def premium_receipt_wrong(message: Message, session: AsyncSession) -> None:
    user = await message_user(message, session)
    if user is None:
        return
    await message.answer(t("premium_receipt_need_file", user.language))


async def _accept_receipt(
    message: Message,
    session: AsyncSession,
    state: FSMContext,
    bot: Bot,
    *,
    kind: str,
    file_id: str,
) -> None:
    user = await message_user(message, session)
    if user is None:
        await state.clear()
        return
    data = await state.get_data()
    order_id = data.get("order_id")
    prompt_message_id = data.get("prompt_message_id")
    if not order_id:
        await state.clear()
        return
    order = await attach_receipt(session, order_id, user.tg_id, file_id, kind)
    if order is None:
        await state.clear()
        await message.answer("—", reply_markup=main_menu_kb(user.language))
        return
    if prompt_message_id:
        try:
            await bot.edit_message_reply_markup(
                chat_id=message.chat.id,
                message_id=prompt_message_id,
                reply_markup=None,
            )
        except TelegramAPIError:
            pass
    await state.clear()
    await message.answer(
        t("premium_paid_thanks", user.language, order_id=order.id),
        reply_markup=main_menu_kb(user.language),
    )
    await _notify_admins_receipt(bot, user, order, kind, file_id)


@router.callback_query(F.data == "prem:back")
async def premium_back(
    callback: CallbackQuery, session: AsyncSession, state: FSMContext
) -> None:
    ctx = await callback_context(callback, session)
    if ctx is None:
        return
    user, message = ctx
    await state.clear()
    await callback.answer()
    try:
        await message.edit_reply_markup(reply_markup=None)
    except Exception:
        pass
    await show_main_menu(message, user)
