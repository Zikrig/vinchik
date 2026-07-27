from aiogram import Bot, F, Router
from aiogram.exceptions import TelegramAPIError
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup
from sqlalchemy.ext.asyncio import AsyncSession

from config import settings
from handlers.common import load_user, show_main_menu
from locales import t
from services.premium import create_order, list_active_plans
from services.settings_service import get_payment_info
from services.users import is_premium

router = Router()


def _premium_pay_kb(lang: str, order_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text=t("btn_i_paid", lang),
                    callback_data=f"prem:paid:{order_id}",
                )
            ],
            [InlineKeyboardButton(text=t("back", lang), callback_data="prem:back")],
        ]
    )


@router.callback_query(F.data == "menu:premium")
async def premium_menu(callback: CallbackQuery, session: AsyncSession) -> None:
    user = await load_user(session, callback.from_user.id)
    assert user and callback.message
    lang = user.language
    await callback.answer()
    if is_premium(user) and user.premium_until:
        await callback.message.answer(
            t("premium_active_until", lang, dt=user.premium_until.strftime("%Y-%m-%d %H:%M UTC"))
        )
    plans = await list_active_plans(session)
    rows = [
        [InlineKeyboardButton(text=f"💎 {p.title} — {p.price_text}", callback_data=f"prem:buy:{p.id}")]
        for p in plans
    ]
    rows.append([InlineKeyboardButton(text=t("back", lang), callback_data="prem:back")])
    await callback.message.answer(
        t("premium_choose", lang) if plans else t("premium_title", lang),
        reply_markup=InlineKeyboardMarkup(inline_keyboard=rows),
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
        t("premium_paid_thanks", user.language, order_id=order_id)
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
