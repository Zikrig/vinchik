from aiogram import F, Router
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery
from sqlalchemy.ext.asyncio import AsyncSession

from handlers.common import load_user, show_main_menu, show_my_profile
from handlers.profile import begin_profile_flow
from keyboards.inline import language_kb, main_menu_kb, settings_kb, stop_confirm_kb
from locales import t
from services.channels import format_channels_lines, list_active_channels
from services.users import is_premium
from states.profile import ProfileStates

router = Router()


def _settings_channels_text(lang: str, channels, *, premium: bool) -> str:
    if not channels:
        body = t("settings_channels_empty", lang)
    else:
        body = f"{t('settings_channels_title', lang)}\n{format_channels_lines(channels)}"
    if premium:
        body = f"{body}\n\n{t('settings_channels_premium_note', lang)}"
    return body


@router.callback_query(F.data == "menu:root")
async def menu_root(callback: CallbackQuery, session: AsyncSession) -> None:
    user = await load_user(session, callback.from_user.id)
    assert user and callback.message
    if user.is_blocked:
        await callback.answer(t("you_are_blocked", user.language), show_alert=True)
        return
    await callback.answer()
    await show_main_menu(callback.message, user)


@router.callback_query(F.data == "menu:my")
async def menu_my(callback: CallbackQuery, session: AsyncSession) -> None:
    user = await load_user(session, callback.from_user.id)
    assert user and user.profile and callback.message
    await callback.answer()
    await show_my_profile(callback.message, user, user.profile)


@router.callback_query(F.data == "menu:settings")
async def menu_settings(callback: CallbackQuery, session: AsyncSession) -> None:
    user = await load_user(session, callback.from_user.id)
    assert user and callback.message
    await callback.answer()
    await callback.message.answer(
        t("settings_title", user.language), reply_markup=settings_kb(user.language)
    )


@router.callback_query(F.data == "settings:channels")
async def settings_channels(callback: CallbackQuery, session: AsyncSession) -> None:
    user = await load_user(session, callback.from_user.id)
    assert user and callback.message
    channels = await list_active_channels(session)
    await callback.answer()
    await callback.message.answer(
        _settings_channels_text(
            user.language, channels, premium=is_premium(user)
        ),
        reply_markup=settings_kb(user.language),
        disable_web_page_preview=True,
    )


@router.callback_query(F.data == "settings:lang")
async def settings_lang(callback: CallbackQuery, session: AsyncSession) -> None:
    user = await load_user(session, callback.from_user.id)
    assert user and callback.message
    await callback.answer()
    await callback.message.answer(t("choose_language", user.language), reply_markup=language_kb())


@router.callback_query(F.data == "menu:stop")
async def menu_stop(callback: CallbackQuery, session: AsyncSession) -> None:
    user = await load_user(session, callback.from_user.id)
    assert user and callback.message
    await callback.answer()
    await callback.message.answer(
        t("stop_confirm", user.language), reply_markup=stop_confirm_kb(user.language)
    )


@router.callback_query(F.data == "stop:yes")
async def stop_yes(callback: CallbackQuery, session: AsyncSession) -> None:
    user = await load_user(session, callback.from_user.id)
    assert user and user.profile and callback.message
    user.profile.is_active = False
    await session.commit()
    await callback.answer()
    await callback.message.answer(
        t("profile_disabled", user.language), reply_markup=main_menu_kb(user.language)
    )


@router.callback_query(F.data == "profile:refill")
async def profile_refill(
    callback: CallbackQuery, session: AsyncSession, state: FSMContext
) -> None:
    user = await load_user(session, callback.from_user.id)
    assert user and callback.message
    await callback.answer()
    await begin_profile_flow(callback.message, session, state, user, refill=True)


@router.callback_query(F.data == "profile:edit_photo")
async def profile_edit_photo(
    callback: CallbackQuery, session: AsyncSession, state: FSMContext
) -> None:
    user = await load_user(session, callback.from_user.id)
    assert user and callback.message
    await state.set_state(ProfileStates.edit_photo)
    await callback.answer()
    await callback.message.answer(t("ask_photo", user.language))


@router.callback_query(F.data == "profile:edit_text")
async def profile_edit_text(
    callback: CallbackQuery, session: AsyncSession, state: FSMContext
) -> None:
    user = await load_user(session, callback.from_user.id)
    assert user and callback.message
    await state.set_state(ProfileStates.edit_text)
    await callback.answer()
    await callback.message.answer(t("ask_about", user.language))
