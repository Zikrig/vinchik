from aiogram import Bot, F, Router
from aiogram.filters import Command
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
    is_registration_only,
    set_setting,
)

router = Router()


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


async def _root_text(session: AsyncSession) -> str:
    pending = await list_pending_orders(session)
    reg_only = await is_registration_only(session)
    return (
        "Админка\n"
        f"Заявок на оплату: {len(pending)}\n"
        f"Soft-launch (только регистрация): {reg_only}\n\n"
        "Баны и константы оплаты — в веб-админке."
    )


async def _settings_text(session: AsyncSession) -> str:
    limit = await get_daily_like_limit(session)
    dist = await get_max_distance_km(session)
    pay = await get_payment_info(session)
    return (
        "⚙️ Настройки\n\n"
        f"Лимит лайков / сутки UTC: {limit}\n"
        f"Макс. радиус ленты: {dist:g} км\n\n"
        f"Карта: {pay['card']}\n"
        f"Время проверки: {pay['check_time']}\n"
        f"Менеджер: {pay['manager']}\n\n"
        "Карту / время / менеджера меняй в веб-админке."
    )


def _settings_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="Лимит 20", callback_data="adm:limit:20"),
                InlineKeyboardButton(text="Лимит 50", callback_data="adm:limit:50"),
                InlineKeyboardButton(text="Лимит 100", callback_data="adm:limit:100"),
            ],
            [
                InlineKeyboardButton(text="100 км", callback_data="adm:dist:100"),
                InlineKeyboardButton(text="500 км", callback_data="adm:dist:500"),
                InlineKeyboardButton(text="1000 км", callback_data="adm:dist:1000"),
            ],
            [
                InlineKeyboardButton(text="5000 км", callback_data="adm:dist:5000"),
                InlineKeyboardButton(text="20000 км", callback_data="adm:dist:20000"),
            ],
            [InlineKeyboardButton(text="⬅️ Назад", callback_data="adm:root")],
        ]
    )


@router.message(Command("admin"))
async def admin_cmd(message: Message, session: AsyncSession) -> None:
    assert message.from_user
    if not _is_admin(message.from_user.id):
        await message.answer(t("no_access", "ru"))
        return
    await message.answer(await _root_text(session), reply_markup=_root_kb())


@router.callback_query(F.data == "adm:root")
async def adm_root(callback: CallbackQuery, session: AsyncSession) -> None:
    if not _is_admin(callback.from_user.id):
        await callback.answer(t("no_access", "ru"), show_alert=True)
        return
    await callback.answer()
    assert callback.message
    await callback.message.edit_text(await _root_text(session), reply_markup=_root_kb())


@router.callback_query(F.data == "adm:settings")
async def adm_settings(callback: CallbackQuery, session: AsyncSession) -> None:
    if not _is_admin(callback.from_user.id):
        await callback.answer(t("no_access", "ru"), show_alert=True)
        return
    await callback.answer()
    assert callback.message
    await callback.message.edit_text(
        await _settings_text(session), reply_markup=_settings_kb()
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


@router.callback_query(F.data.startswith("adm:limit:"))
async def adm_limit(callback: CallbackQuery, session: AsyncSession) -> None:
    if not _is_admin(callback.from_user.id):
        await callback.answer(t("no_access", "ru"), show_alert=True)
        return
    value = callback.data.split(":")[2]  # type: ignore[union-attr]
    await set_setting(session, "daily_like_limit", value)
    await callback.answer(f"limit={value}")
    assert callback.message
    await callback.message.edit_text(
        await _settings_text(session), reply_markup=_settings_kb()
    )


@router.callback_query(F.data.startswith("adm:dist:"))
async def adm_dist(callback: CallbackQuery, session: AsyncSession) -> None:
    if not _is_admin(callback.from_user.id):
        await callback.answer(t("no_access", "ru"), show_alert=True)
        return
    value = callback.data.split(":")[2]  # type: ignore[union-attr]
    capped = min(max(float(value), 1.0), 20000.0)
    await set_setting(session, "max_distance_km", str(capped))
    await callback.answer(f"distance={capped:g}")
    assert callback.message
    await callback.message.edit_text(
        await _settings_text(session), reply_markup=_settings_kb()
    )
