from __future__ import annotations

import html

from aiogram import Bot, F, Router
from aiogram.filters import StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.types import (
    CallbackQuery,
    CopyTextButton,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
)
from sqlalchemy.ext.asyncio import AsyncSession

from handlers.admin import _btn, _is_admin, _redraw
from locales import t
from services.tracking_links import (
    click_counts,
    create_link,
    delete_link,
    format_stats_message,
    get_link,
    list_links,
    public_url,
    rename_link,
    resolve_range,
)
from states.admin import AdminStates

router = Router()


def _links_cancel_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="❌ Отмена", callback_data="adm:links")]
        ]
    )


async def _links_list_view(session: AsyncSession) -> tuple[str, InlineKeyboardMarkup]:
    links = await list_links(session)
    rows: list[list[InlineKeyboardButton]] = [
        [InlineKeyboardButton(text="➕ Создать ссылку", callback_data="adm:links:new")],
        [InlineKeyboardButton(text="📊 Статистика", callback_data="adm:links:stats")],
    ]
    if not links:
        text = "🔗 Ссылки\n\nПока пусто. Создай первую — получишь полную t.me-ссылку с аргументом."
    else:
        text = f"🔗 Ссылки ({len(links)})\n\nВыбери ссылку:"
        for link in links:
            label = link.name or f"#{link.id}"
            if len(label) > 48:
                label = label[:45] + "…"
            rows.append(
                [_btn(f"🔗 {label}", f"adm:link:{link.id}")]
            )
    rows.append([InlineKeyboardButton(text="⬅️ Назад", callback_data="adm:root")])
    return text, InlineKeyboardMarkup(inline_keyboard=rows)


async def _link_detail_view(session: AsyncSession, link_id: int) -> tuple[str, InlineKeyboardMarkup] | None:
    link = await get_link(session, link_id)
    if link is None:
        return None
    url = public_url(link.code)
    text = (
        f"🔗 {html.escape(link.name)}\n\n"
        f"Код: <code>{html.escape(link.code)}</code>\n"
        f"Ссылка:\n<code>{html.escape(url)}</code>"
    )
    kb = InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="✏️ Название", callback_data=f"adm:link:ren:{link.id}")],
            [
                InlineKeyboardButton(
                    text="📋 Копировать ссылку",
                    copy_text=CopyTextButton(text=url),
                )
            ],
            [InlineKeyboardButton(text="🗑 Удалить", callback_data=f"adm:link:del:{link.id}")],
            [InlineKeyboardButton(text="⬅️ К списку", callback_data="adm:links")],
        ]
    )
    return text, kb


@router.callback_query(F.data == "adm:links")
async def adm_links(
    callback: CallbackQuery, session: AsyncSession, state: FSMContext, bot: Bot
) -> None:
    if not _is_admin(callback.from_user.id):
        await callback.answer(t("no_access", "ru"), show_alert=True)
        return
    await state.clear()
    await callback.answer()
    text, kb = await _links_list_view(session)
    await _redraw(callback, bot, text, kb)


@router.callback_query(F.data == "adm:links:stats")
async def adm_links_stats(
    callback: CallbackQuery, session: AsyncSession, bot: Bot
) -> None:
    if not _is_admin(callback.from_user.id):
        await callback.answer(t("no_access", "ru"), show_alert=True)
        return
    await callback.answer()
    start, end, label = resolve_range(preset="all")
    rows = await click_counts(session, start=start, end=end)
    text = format_stats_message(rows, title=f"Переходы · {label}")
    kb = InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="⬅️ К ссылкам", callback_data="adm:links")],
        ]
    )
    await _redraw(callback, bot, text, kb)


@router.callback_query(F.data == "adm:links:new")
async def adm_links_new(callback: CallbackQuery, state: FSMContext, bot: Bot) -> None:
    if not _is_admin(callback.from_user.id):
        await callback.answer(t("no_access", "ru"), show_alert=True)
        return
    await state.set_state(AdminStates.link_create_name)
    await callback.answer()
    await bot.send_message(
        callback.from_user.id,
        "➕ Новая ссылка\n\n"
        "Пришли название (видно в статистике).\n"
        "Аргумент ?start= по умолчанию — латиница из названия.\n"
        "Свой код: <code>Название | moy_kod</code>",
        reply_markup=_links_cancel_kb(),
    )


@router.message(StateFilter(AdminStates.link_create_name), F.text)
async def adm_link_create_name(
    message: Message, session: AsyncSession, state: FSMContext
) -> None:
    if message.from_user is None or not _is_admin(message.from_user.id):
        return
    raw = message.text or ""
    if "|" in raw:
        name_part, code_part = raw.split("|", 1)
    else:
        name_part, code_part = raw, ""
    try:
        link = await create_link(session, name_part, code_part or None)
    except ValueError as exc:
        await message.answer(str(exc), reply_markup=_links_cancel_kb())
        return
    await state.clear()
    url = public_url(link.code)
    await message.answer(
        f"✅ Создано: <b>{html.escape(link.name)}</b>\n"
        f"Код: <code>{html.escape(link.code)}</code>\n\n"
        f"<code>{html.escape(url)}</code>",
        reply_markup=InlineKeyboardMarkup(
            inline_keyboard=[
                [
                    InlineKeyboardButton(
                        text="📋 Копировать",
                        copy_text=CopyTextButton(text=url),
                    )
                ],
                [InlineKeyboardButton(text="Открыть", callback_data=f"adm:link:{link.id}")],
                [InlineKeyboardButton(text="К списку", callback_data="adm:links")],
            ]
        ),
    )


@router.callback_query(F.data.regexp(r"^adm:link:\d+$"))
async def adm_link_open(
    callback: CallbackQuery, session: AsyncSession, state: FSMContext, bot: Bot
) -> None:
    if not _is_admin(callback.from_user.id):
        await callback.answer(t("no_access", "ru"), show_alert=True)
        return
    link_id = int((callback.data or "").rsplit(":", 1)[-1])
    await state.clear()
    view = await _link_detail_view(session, link_id)
    if view is None:
        await callback.answer("Ссылка не найдена", show_alert=True)
        text, kb = await _links_list_view(session)
        await _redraw(callback, bot, text, kb)
        return
    await callback.answer()
    text, kb = view
    await _redraw(callback, bot, text, kb)


@router.callback_query(F.data.startswith("adm:link:ren:"))
async def adm_link_rename_start(
    callback: CallbackQuery, state: FSMContext, bot: Bot
) -> None:
    if not _is_admin(callback.from_user.id):
        await callback.answer(t("no_access", "ru"), show_alert=True)
        return
    link_id = int((callback.data or "").rsplit(":", 1)[-1])
    await state.set_state(AdminStates.link_rename)
    await state.update_data(link_id=link_id)
    await callback.answer()
    await bot.send_message(
        callback.from_user.id,
        "✏️ Новое название ссылки:",
        reply_markup=_links_cancel_kb(),
    )


@router.message(StateFilter(AdminStates.link_rename), F.text)
async def adm_link_rename_save(
    message: Message, session: AsyncSession, state: FSMContext
) -> None:
    if message.from_user is None or not _is_admin(message.from_user.id):
        return
    data = await state.get_data()
    link_id = int(data.get("link_id") or 0)
    try:
        link = await rename_link(session, link_id, message.text or "")
    except ValueError as exc:
        await message.answer(str(exc), reply_markup=_links_cancel_kb())
        return
    await state.clear()
    if link is None:
        await message.answer("Ссылка не найдена.", reply_markup=_links_cancel_kb())
        return
    view = await _link_detail_view(session, link.id)
    if view is None:
        await message.answer("Готово.", reply_markup=_links_cancel_kb())
        return
    text, kb = view
    await message.answer(text, reply_markup=kb)


@router.callback_query(F.data.startswith("adm:link:del:"))
async def adm_link_delete_ask(
    callback: CallbackQuery, session: AsyncSession, bot: Bot
) -> None:
    if not _is_admin(callback.from_user.id):
        await callback.answer(t("no_access", "ru"), show_alert=True)
        return
    link_id = int((callback.data or "").rsplit(":", 1)[-1])
    link = await get_link(session, link_id)
    if link is None:
        await callback.answer("Уже удалена", show_alert=True)
        return
    await callback.answer()
    text = (
        f"Удалить ссылку «{html.escape(link.name)}»?\n"
        "Статистика переходов тоже пропадёт."
    )
    kb = InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="🗑 Да, удалить", callback_data=f"adm:link:okdel:{link.id}"
                )
            ],
            [InlineKeyboardButton(text="⬅️ Отмена", callback_data=f"adm:link:{link.id}")],
        ]
    )
    await _redraw(callback, bot, text, kb)


@router.callback_query(F.data.startswith("adm:link:okdel:"))
async def adm_link_delete_ok(
    callback: CallbackQuery, session: AsyncSession, bot: Bot
) -> None:
    if not _is_admin(callback.from_user.id):
        await callback.answer(t("no_access", "ru"), show_alert=True)
        return
    link_id = int((callback.data or "").rsplit(":", 1)[-1])
    await delete_link(session, link_id)
    await callback.answer("Удалено")
    text, kb = await _links_list_view(session)
    await _redraw(callback, bot, text, kb)
