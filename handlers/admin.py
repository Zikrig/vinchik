from aiogram import Bot, F, Router
from aiogram.filters import Command, StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message
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

_EDIT_PROMPTS = {
    AdminStates.edit_limit.state: "Новый лимит лайков / сутки UTC (целое ≥ 1):",
    AdminStates.edit_dist.state: "Новый радиус км (1–20000):",
    AdminStates.edit_reshow.state: "Повтор анкеты в днях (0 = никогда; на ленту сейчас не влияет):",
    AdminStates.edit_card.state: "Новая карта для приёма платежей:",
    AdminStates.edit_check_time.state: "Новое время проверки оплаты:",
    AdminStates.edit_manager.state: "Новые контакты менеджера:",
}


def _is_admin(user_id: int) -> bool:
    return user_id in settings.admin_id_set


def _root_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="📋 Заявки Премиум", callback_data="adm:orders")],
            [InlineKeyboardButton(text="⭐ Премиум юзеры", callback_data="adm:premiums")],
            [InlineKeyboardButton(text="🚦 Soft-launch on/off", callback_data="adm:toggle_reg")],
            [InlineKeyboardButton(text="⚙️ Настройки", callback_data="adm:settings")],
        ]
    )


def _settings_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
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


async def _root_text(session: AsyncSession) -> str:
    pending = await list_pending_orders(session)
    reg_only = await is_registration_only(session)
    return (
        "Админка\n"
        f"Заявок на оплату: {len(pending)}\n"
        f"Soft-launch (только регистрация): {reg_only}\n\n"
        "Баны — в веб-админке."
    )


async def _settings_text(session: AsyncSession) -> str:
    limit = await get_daily_like_limit(session)
    dist = await get_max_distance_km(session)
    reshow = await get_profile_reshow_days(session)
    reg_only = await is_registration_only(session)
    pay = await get_payment_info(session)
    return (
        "⚙️ Настройки\n\n"
        f"Лимит лайков / сутки UTC: {limit}\n"
        f"Радиус км (макс. 20000): {dist:g}\n"
        f"Повтор анкеты (дней): {reshow}\n"
        f"Только регистрация (soft-launch): {reg_only}\n\n"
        f"Карта для приёма платежей:\n{pay['card']}\n\n"
        f"Время проверки оплаты:\n{pay['check_time']}\n\n"
        f"Контакты менеджера:\n{pay['manager']}"
    )


@router.message(Command("admin"))
async def admin_cmd(message: Message, session: AsyncSession, state: FSMContext) -> None:
    assert message.from_user
    if not _is_admin(message.from_user.id):
        await message.answer(t("no_access", "ru"))
        return
    await state.clear()
    await message.answer(await _root_text(session), reply_markup=_root_kb())


@router.callback_query(F.data == "adm:root")
async def adm_root(callback: CallbackQuery, session: AsyncSession, state: FSMContext) -> None:
    if not _is_admin(callback.from_user.id):
        await callback.answer(t("no_access", "ru"), show_alert=True)
        return
    await state.clear()
    await callback.answer()
    assert callback.message
    await callback.message.edit_text(await _root_text(session), reply_markup=_root_kb())


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
    await callback.message.edit_text(
        await _settings_text(session), reply_markup=_settings_kb()
    )


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
    await message.answer(
        "✅ Сохранено.\n\n" + await _settings_text(session),
        reply_markup=_settings_kb(),
    )


@router.callback_query(F.data == "adm:orders")
async def adm_orders(callback: CallbackQuery, session: AsyncSession) -> None:
    if not _is_admin(callback.from_user.id):
        await callback.answer(t("no_access", "ru"), show_alert=True)
        return
    pending = await list_pending_orders(session)
    await callback.answer()
    assert callback.message
    if not pending:
        await callback.message.answer("Нет заявок.")
        return
    for order in pending[:20]:
        kb = InlineKeyboardMarkup(
            inline_keyboard=[
                [
                    InlineKeyboardButton(
                        text="✅", callback_data=f"adm:ok:{order.id}"
                    ),
                    InlineKeyboardButton(
                        text="❌", callback_data=f"adm:no:{order.id}"
                    ),
                ]
            ]
        )
        await callback.message.answer(
            f"Заявка #{order.id} user={order.user_id} plan={order.plan_id}",
            reply_markup=kb,
        )


@router.callback_query(F.data.startswith("adm:ok:"))
async def adm_ok(callback: CallbackQuery, session: AsyncSession, bot: Bot) -> None:
    if not _is_admin(callback.from_user.id):
        await callback.answer(t("no_access", "ru"), show_alert=True)
        return
    order_id = int(callback.data.split(":")[2])  # type: ignore[union-attr]
    result = await approve_order(session, order_id, callback.from_user.id)
    await callback.answer("OK")
    if result:
        order, user = result
        await notify_premium_activated(bot, user)
        assert callback.message
        await callback.message.edit_text(f"Заявка #{order.id} одобрена")


@router.callback_query(F.data.startswith("adm:no:"))
async def adm_no(callback: CallbackQuery, session: AsyncSession) -> None:
    if not _is_admin(callback.from_user.id):
        await callback.answer(t("no_access", "ru"), show_alert=True)
        return
    order_id = int(callback.data.split(":")[2])  # type: ignore[union-attr]
    order = await reject_order(session, order_id, callback.from_user.id)
    await callback.answer("Rejected")
    if order and callback.message:
        await callback.message.edit_text(f"Заявка #{order.id} отклонена")


@router.callback_query(F.data == "adm:premiums")
async def adm_premiums(callback: CallbackQuery, session: AsyncSession) -> None:
    if not _is_admin(callback.from_user.id):
        await callback.answer(t("no_access", "ru"), show_alert=True)
        return
    users = await list_premium_users(session)
    await callback.answer()
    assert callback.message
    if not users:
        await callback.message.answer("Нет активных премиум.")
        return
    lines = [
        f"{u.tg_id} @{u.username or '-'} до {u.premium_until}" for u in users[:50]
    ]
    await callback.message.answer("\n".join(lines))


@router.callback_query(F.data == "adm:toggle_reg")
async def adm_toggle_reg(callback: CallbackQuery, session: AsyncSession) -> None:
    if not _is_admin(callback.from_user.id):
        await callback.answer(t("no_access", "ru"), show_alert=True)
        return
    current = await is_registration_only(session)
    await set_setting(session, "registration_only", "false" if current else "true")
    await callback.answer(f"registration_only={not current}")
    assert callback.message
    await callback.message.edit_text(await _root_text(session), reply_markup=_root_kb())
