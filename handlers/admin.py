from aiogram import Bot, F, Router
from aiogram.filters import Command, StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message
from datetime import UTC, datetime
from zoneinfo import ZoneInfo

from sqlalchemy.ext.asyncio import AsyncSession

from config import settings
from locales import t
from services.premium import (
    approve_order,
    list_pending_orders,
    list_premium_users,
    notify_premium_activated,
    reject_order,
)
from services.settings_service import (
    get_daily_like_limit,
    get_max_distance_km,
    get_payment_info,
    get_profile_reshow_days,
    is_registration_only,
    set_setting,
)
from states.admin import AdminStates

router = Router()

_TZ = ZoneInfo("Asia/Dushanbe")
_PREMIUMS_PER_PAGE = 8

_EDIT_PROMPTS = {
    AdminStates.edit_limit.state: "Новый лимит лайков / сутки UTC (целое ≥ 1):",
    AdminStates.edit_dist.state: "Новый радиус км (1–20000):",
    AdminStates.edit_reshow.state: "Повтор анкеты в днях (0 = никогда; пауза в обе стороны после ❤️/👎/💌):",
    AdminStates.edit_card.state: "Новая карта для приёма платежей:",
    AdminStates.edit_check_time.state: "Новое время проверки оплаты:",
    AdminStates.edit_manager.state: "Новые контакты менеджера:",
}


def _is_admin(user_id: int) -> bool:
    return user_id in settings.admin_id_set


def _fmt_dt(dt: datetime | None) -> str:
    if dt is None:
        return "—"
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return dt.astimezone(_TZ).strftime("%d.%m.%Y %H:%M")


def _btn(text: str, data: str) -> InlineKeyboardButton:
    if len(text) > 64:
        text = text[:61] + "…"
    return InlineKeyboardButton(text=text, callback_data=data)


def _root_kb(reg_only: bool, pending_n: int) -> InlineKeyboardMarkup:
    soft = (
        "🟢 Soft-launch · ON"
        if reg_only
        else "🔴 Soft-launch · OFF"
    )
    orders = f"📋 Заявки ({pending_n})" if pending_n else "📋 Заявки"
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text=orders, callback_data="adm:orders:0")],
            [InlineKeyboardButton(text="⭐ Премиум юзеры", callback_data="adm:premiums:0")],
            [InlineKeyboardButton(text=soft, callback_data="adm:toggle_reg")],
            [InlineKeyboardButton(text="⚙️ Настройки", callback_data="adm:settings")],
        ]
    )


def _settings_kb(reg_only: bool) -> InlineKeyboardMarkup:
    soft = (
        "🟢 Soft-launch · ON"
        if reg_only
        else "🔴 Soft-launch · OFF"
    )
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text=soft, callback_data="adm:toggle_reg")],
            [InlineKeyboardButton(text="Лимит лайков", callback_data="adm:edit:limit")],
            [InlineKeyboardButton(text="Радиус км", callback_data="adm:edit:dist")],
            [InlineKeyboardButton(text="Повтор анкеты (дней)", callback_data="adm:edit:reshow")],
            [InlineKeyboardButton(text="Карта оплаты", callback_data="adm:edit:card")],
            [InlineKeyboardButton(text="Время проверки", callback_data="adm:edit:check_time")],
            [InlineKeyboardButton(text="Контакты менеджера", callback_data="adm:edit:manager")],
            [InlineKeyboardButton(text="⬅️ Назад", callback_data="adm:root")],
        ]
    )


def _cancel_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="❌ Отмена", callback_data="adm:settings")],
        ]
    )


def _premiums_page_kb(users, page: int, total_pages: int) -> InlineKeyboardMarkup:
    rows: list[list[InlineKeyboardButton]] = []
    for u in users:
        uname = f"@{u.username}" if u.username else str(u.tg_id)
        label = f"{uname} · до {_fmt_dt(u.premium_until)}"
        rows.append([_btn(label, "adm:noop")])
    nav: list[InlineKeyboardButton] = []
    if page > 0:
        nav.append(InlineKeyboardButton(text="◀️", callback_data=f"adm:premiums:{page - 1}"))
    nav.append(_btn(f"{page + 1}/{total_pages}", "adm:noop"))
    if page + 1 < total_pages:
        nav.append(InlineKeyboardButton(text="▶️", callback_data=f"adm:premiums:{page + 1}"))
    rows.append(nav)
    rows.append([InlineKeyboardButton(text="⬅️ Назад", callback_data="adm:root")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def _order_kb(order_id: int, index: int, total: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="✅ Подтвердить",
                    callback_data=f"adm:ok:{order_id}:{index}",
                ),
                InlineKeyboardButton(
                    text="❌ Отклонить",
                    callback_data=f"adm:no:{order_id}:{index}",
                ),
            ],
            [InlineKeyboardButton(text="⬅️ Назад", callback_data="adm:root")],
        ]
    )


async def _root_view(session: AsyncSession) -> tuple[str, InlineKeyboardMarkup]:
    pending = await list_pending_orders(session)
    reg_only = await is_registration_only(session)
    soft = "🟢 ON (только регистрация)" if reg_only else "🔴 OFF (лента открыта)"
    text = (
        "Админка Vinchik\n\n"
        f"Soft-launch: {soft}\n"
        f"Заявок на оплату: {len(pending)}\n\n"
        "Баны и аккаунты — в веб-админке."
    )
    return text, _root_kb(reg_only, len(pending))


async def _settings_view(session: AsyncSession) -> tuple[str, InlineKeyboardMarkup]:
    limit = await get_daily_like_limit(session)
    dist = await get_max_distance_km(session)
    reshow = await get_profile_reshow_days(session)
    reg_only = await is_registration_only(session)
    pay = await get_payment_info(session)
    soft = "🟢 ON" if reg_only else "🔴 OFF"
    text = (
        "⚙️ Настройки\n\n"
        f"Soft-launch: {soft}\n"
        f"Лимит лайков / сутки UTC: {limit}\n"
        f"Радиус км: {dist:g}\n"
        f"Повтор анкеты (дней): {reshow}\n\n"
        f"Карта:\n{pay['card']}\n\n"
        f"Время проверки:\n{pay['check_time']}\n\n"
        f"Менеджер:\n{pay['manager']}"
    )
    return text, _settings_kb(reg_only)


async def _render_order(
    session: AsyncSession, index: int
) -> tuple[str, InlineKeyboardMarkup]:
    pending = await list_pending_orders(session)
    if not pending:
        return (
            "Нет заявок на оплату.",
            InlineKeyboardMarkup(
                inline_keyboard=[
                    [InlineKeyboardButton(text="⬅️ Назад", callback_data="adm:root")]
                ]
            ),
        )
    index = max(0, min(index, len(pending) - 1))
    order = pending[index]
    text = (
        f"Заявка {index + 1}/{len(pending)}\n\n"
        f"#{order.id}\n"
        f"user: {order.user_id}\n"
        f"plan: {order.plan_id}\n"
        f"создана: {_fmt_dt(order.created_at)}"
    )
    return text, _order_kb(order.id, index, len(pending))


async def _render_premiums(
    session: AsyncSession, page: int
) -> tuple[str, InlineKeyboardMarkup]:
    users = await list_premium_users(session)
    if not users:
        return (
            "Нет активных премиум.",
            InlineKeyboardMarkup(
                inline_keyboard=[
                    [InlineKeyboardButton(text="⬅️ Назад", callback_data="adm:root")]
                ]
            ),
        )
    total_pages = max(1, (len(users) + _PREMIUMS_PER_PAGE - 1) // _PREMIUMS_PER_PAGE)
    page = max(0, min(page, total_pages - 1))
    chunk = users[page * _PREMIUMS_PER_PAGE : (page + 1) * _PREMIUMS_PER_PAGE]
    text = f"⭐ Премиум юзеры · стр. {page + 1}/{total_pages} · всего {len(users)}"
    return text, _premiums_page_kb(chunk, page, total_pages)


@router.message(Command("admin"))
async def admin_cmd(message: Message, session: AsyncSession, state: FSMContext) -> None:
    assert message.from_user
    if not _is_admin(message.from_user.id):
        await message.answer(t("no_access", "ru"))
        return
    await state.clear()
    text, kb = await _root_view(session)
    await message.answer(text, reply_markup=kb)


@router.callback_query(F.data == "adm:noop")
async def adm_noop(callback: CallbackQuery) -> None:
    await callback.answer()


@router.callback_query(F.data == "adm:root")
async def adm_root(callback: CallbackQuery, session: AsyncSession, state: FSMContext) -> None:
    if not _is_admin(callback.from_user.id):
        await callback.answer(t("no_access", "ru"), show_alert=True)
        return
    await state.clear()
    await callback.answer()
    assert callback.message
    text, kb = await _root_view(session)
    await callback.message.edit_text(text, reply_markup=kb)


@router.callback_query(F.data == "adm:settings")
async def adm_settings(
    callback: CallbackQuery, session: AsyncSession, state: FSMContext
) -> None:
    if not _is_admin(callback.from_user.id):
        await callback.answer(t("no_access", "ru"), show_alert=True)
        return
    await state.clear()
    await callback.answer()
    assert callback.message
    text, kb = await _settings_view(session)
    await callback.message.edit_text(text, reply_markup=kb)


@router.callback_query(F.data.startswith("adm:edit:"))
async def adm_edit_start(callback: CallbackQuery, state: FSMContext) -> None:
    if not _is_admin(callback.from_user.id):
        await callback.answer(t("no_access", "ru"), show_alert=True)
        return
    field = callback.data.split(":")[2]  # type: ignore[union-attr]
    mapping = {
        "limit": AdminStates.edit_limit,
        "dist": AdminStates.edit_dist,
        "reshow": AdminStates.edit_reshow,
        "card": AdminStates.edit_card,
        "check_time": AdminStates.edit_check_time,
        "manager": AdminStates.edit_manager,
    }
    st = mapping.get(field)
    if st is None:
        await callback.answer("—", show_alert=True)
        return
    await state.set_state(st)
    await callback.answer()
    assert callback.message
    await callback.message.answer(_EDIT_PROMPTS[st.state], reply_markup=_cancel_kb())


@router.message(StateFilter(AdminStates), F.text)
async def adm_edit_value(
    message: Message, session: AsyncSession, state: FSMContext
) -> None:
    assert message.from_user
    if not _is_admin(message.from_user.id):
        await state.clear()
        return
    current = await state.get_state()
    raw = (message.text or "").strip()
    try:
        if current == AdminStates.edit_limit.state:
            value = max(1, int(raw))
            await set_setting(session, "daily_like_limit", str(value))
        elif current == AdminStates.edit_dist.state:
            value = min(max(float(raw.replace(",", ".")), 1.0), 20000.0)
            await set_setting(session, "max_distance_km", str(value))
        elif current == AdminStates.edit_reshow.state:
            value = max(0, int(raw))
            await set_setting(session, "profile_reshow_days", str(value))
        elif current == AdminStates.edit_card.state:
            if not raw:
                raise ValueError("empty")
            await set_setting(session, "payment_card", raw)
        elif current == AdminStates.edit_check_time.state:
            if not raw:
                raise ValueError("empty")
            await set_setting(session, "payment_check_time", raw)
        elif current == AdminStates.edit_manager.state:
            if not raw:
                raise ValueError("empty")
            await set_setting(session, "manager_contact", raw)
        else:
            await state.clear()
            return
    except ValueError:
        await message.answer("Некорректное значение. Попробуй ещё раз или отмени.")
        return

    await state.clear()
    text, kb = await _settings_view(session)
    await message.answer("✅ Сохранено.\n\n" + text, reply_markup=kb)


@router.callback_query(F.data.startswith("adm:orders:"))
async def adm_orders(callback: CallbackQuery, session: AsyncSession) -> None:
    if not _is_admin(callback.from_user.id):
        await callback.answer(t("no_access", "ru"), show_alert=True)
        return
    index = int(callback.data.split(":")[2])  # type: ignore[union-attr]
    text, kb = await _render_order(session, index)
    await callback.answer()
    assert callback.message
    await callback.message.edit_text(text, reply_markup=kb)


@router.callback_query(F.data.startswith("adm:ok:"))
async def adm_ok(callback: CallbackQuery, session: AsyncSession, bot: Bot) -> None:
    if not _is_admin(callback.from_user.id):
        await callback.answer(t("no_access", "ru"), show_alert=True)
        return
    parts = callback.data.split(":")  # type: ignore[union-attr]
    order_id = int(parts[2])
    index = int(parts[3]) if len(parts) > 3 else 0
    result = await approve_order(session, order_id, callback.from_user.id)
    if result:
        order, user = result
        await notify_premium_activated(bot, user)
        await callback.answer("Одобрено")
    else:
        await callback.answer("Уже обработана", show_alert=True)
    assert callback.message
    text, kb = await _render_order(session, index)
    await callback.message.edit_text(text, reply_markup=kb)


@router.callback_query(F.data.startswith("adm:no:"))
async def adm_no(callback: CallbackQuery, session: AsyncSession) -> None:
    if not _is_admin(callback.from_user.id):
        await callback.answer(t("no_access", "ru"), show_alert=True)
        return
    parts = callback.data.split(":")  # type: ignore[union-attr]
    order_id = int(parts[2])
    index = int(parts[3]) if len(parts) > 3 else 0
    order = await reject_order(session, order_id, callback.from_user.id)
    if order:
        await callback.answer("Отклонено")
    else:
        await callback.answer("Уже обработана", show_alert=True)
    assert callback.message
    text, kb = await _render_order(session, index)
    await callback.message.edit_text(text, reply_markup=kb)


@router.callback_query(F.data.startswith("adm:premiums:"))
async def adm_premiums(callback: CallbackQuery, session: AsyncSession) -> None:
    if not _is_admin(callback.from_user.id):
        await callback.answer(t("no_access", "ru"), show_alert=True)
        return
    page = int(callback.data.split(":")[2])  # type: ignore[union-attr]
    text, kb = await _render_premiums(session, page)
    await callback.answer()
    assert callback.message
    await callback.message.edit_text(text, reply_markup=kb)


@router.callback_query(F.data == "adm:toggle_reg")
async def adm_toggle_reg(callback: CallbackQuery, session: AsyncSession) -> None:
    if not _is_admin(callback.from_user.id):
        await callback.answer(t("no_access", "ru"), show_alert=True)
        return
    current = await is_registration_only(session)
    await set_setting(session, "registration_only", "false" if current else "true")
    now_on = not current
    await callback.answer("Soft-launch ON" if now_on else "Soft-launch OFF")
    assert callback.message
    msg = callback.message.text or ""
    if msg.startswith("⚙️"):
        text, kb = await _settings_view(session)
    else:
        text, kb = await _root_view(session)
    await callback.message.edit_text(text, reply_markup=kb)
