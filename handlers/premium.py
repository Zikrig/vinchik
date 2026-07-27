from aiogram import F, Router
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup
from sqlalchemy.ext.asyncio import AsyncSession

from handlers.common import load_user
from locales import t
from services.premium import create_order, list_active_plans
from services.settings_service import get_payment_info
from services.users import is_premium

router = Router()


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
    await callback.message.answer(
        t("premium_choose", lang) if plans else t("premium_title", lang),
        reply_markup=InlineKeyboardMarkup(inline_keyboard=rows) if rows else None,
    )


@router.callback_query(F.data.startswith("prem:buy:"))
async def premium_buy(callback: CallbackQuery, session: AsyncSession) -> None:
    user = await load_user(session, callback.from_user.id)
    assert user and callback.data and callback.message
    plan_id = int(callback.data.split(":")[2])
    order = await create_order(session, user.tg_id, plan_id)
    pay = await get_payment_info(session)
    await callback.answer()
    await callback.message.answer(
        t(
            "premium_pay",
            user.language,
            order_id=order.id,
            manager=pay["manager"],
            card=pay["card"],
            check_time=pay["check_time"],
        )
    )
