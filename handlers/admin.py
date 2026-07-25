from aiogram import Bot, F, Router
from aiogram.filters import Command
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message
from sqlalchemy.ext.asyncio import AsyncSession

from config import settings
from locales import t
from services.browse import profile_caption
from services.media import as_photo_input
from services.premium import (
    approve_order,
    list_pending_orders,
    list_premium_users,
    reject_order,
)
from services.reports import list_blocked_users, unban_user
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


@router.message(Command("admin"))
async def admin_cmd(message: Message, session: AsyncSession) -> None:
    assert message.from_user
    if not _is_admin(message.from_user.id):
        await message.answer(t("no_access", "ru"))
        return
    limit = await get_daily_like_limit(session)
    dist = await get_max_distance_km(session)
    reg_only = await is_registration_only(session)
    pay = await get_payment_info(session)
    pending = await list_pending_orders(session)
    text = (
        f"Админка\n"
        f"Лимит лайков/сутки UTC: {limit}\n"
        f"Радиус км: {dist}\n"
        f"Только регистрация: {reg_only}\n"
        f"Карта: {pay['card']}\n"
        f"Время проверки: {pay['check_time']}\n"
        f"Менеджер: {pay['manager']}\n"
        f"Заявок на оплату: {len(pending)}\n\n"
        f"Карту / время / менеджера меняй в веб-админке."
    )
    kb = InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="Заявки Премиум", callback_data="adm:orders")],
            [InlineKeyboardButton(text="Премиум юзеры", callback_data="adm:premiums")],
            [InlineKeyboardButton(text="Soft-launch on/off", callback_data="adm:toggle_reg")],
            [InlineKeyboardButton(text="Лимит = 50", callback_data="adm:limit:50")],
            [InlineKeyboardButton(text="Лимит = 20", callback_data="adm:limit:20")],
            [InlineKeyboardButton(text="Радиус 50 км", callback_data="adm:dist:50")],
            [InlineKeyboardButton(text="Радиус 100 км", callback_data="adm:dist:100")],
            [InlineKeyboardButton(text="Заблокированные", callback_data="adm:blocked")],
        ]
    )
    await message.answer(text, reply_markup=kb)


@router.callback_query(F.data == "adm:blocked")
async def adm_blocked(callback: CallbackQuery, session: AsyncSession) -> None:
    if not _is_admin(callback.from_user.id):
        await callback.answer(t("no_access", "ru"), show_alert=True)
        return
    rows = await list_blocked_users(session)
    await callback.answer()
    assert callback.message
    if not rows:
        await callback.message.answer("Заблокированных нет.")
        return
    for user, profile, reports_n in rows[:30]:
        caption = (
            f"🚫 {user.tg_id} @{user.username or '-'}\n"
            f"Жалоб за 3 мес: {reports_n}\n"
            f"blocked_at: {user.blocked_at}\n"
        )
        if profile:
            caption += profile_caption(profile)
            caption += (
                f"\nпол: {profile.gender}\nищет: {profile.looking_for}\n"
                f"geo: {profile.lat}, {profile.lon}"
            )
        kb = InlineKeyboardMarkup(
            inline_keyboard=[
                [
                    InlineKeyboardButton(
                        text="Разбанить",
                        callback_data=f"adm:unban:{user.tg_id}",
                    )
                ]
            ]
        )
        photo = as_photo_input(profile.photo_file_id) if profile else None
        if photo is not None:
            await callback.message.answer_photo(
                photo, caption=caption[:1024], reply_markup=kb
            )
        else:
            await callback.message.answer(caption, reply_markup=kb)


@router.callback_query(F.data.startswith("adm:unban:"))
async def adm_unban(callback: CallbackQuery, session: AsyncSession) -> None:
    if not _is_admin(callback.from_user.id):
        await callback.answer(t("no_access", "ru"), show_alert=True)
        return
    user_id = int(callback.data.split(":")[2])  # type: ignore[union-attr]
    user = await unban_user(session, user_id)
    await callback.answer("OK" if user else "fail")
    if callback.message:
        try:
            await callback.message.edit_reply_markup(reply_markup=None)
        except Exception:
            pass
        await callback.message.answer(f"Разбанен {user_id}")


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
        try:
            await bot.send_message(user.tg_id, t("premium_activated", user.language))
        except Exception:
            pass
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


@router.callback_query(F.data.startswith("adm:limit:"))
async def adm_limit(callback: CallbackQuery, session: AsyncSession) -> None:
    if not _is_admin(callback.from_user.id):
        await callback.answer(t("no_access", "ru"), show_alert=True)
        return
    value = callback.data.split(":")[2]  # type: ignore[union-attr]
    await set_setting(session, "daily_like_limit", value)
    await callback.answer(f"limit={value}")


@router.callback_query(F.data.startswith("adm:dist:"))
async def adm_dist(callback: CallbackQuery, session: AsyncSession) -> None:
    if not _is_admin(callback.from_user.id):
        await callback.answer(t("no_access", "ru"), show_alert=True)
        return
    value = callback.data.split(":")[2]  # type: ignore[union-attr]
    await set_setting(session, "max_distance_km", value)
    await callback.answer(f"distance={value}")
