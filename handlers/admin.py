import html

from aiogram import Bot, F, Router
from aiogram.exceptions import TelegramBadRequest
from aiogram.filters import Command, StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message
from datetime import UTC, datetime
from zoneinfo import ZoneInfo

from sqlalchemy.ext.asyncio import AsyncSession

from config import settings
from locales import t
from services.channels import (
    ChannelResolveError,
    add_resolved_channel,
    delete_channel,
    list_all_channels,
    resolve_channel_forward,
    resolve_channel_ref,
    toggle_channel,
)
from services.admin_tools import count_profiles_by_gender
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
    get_welcome_post,
    is_registration_only,
    set_setting,
    set_welcome_post,
    welcome_post_configured,
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
    AdminStates.edit_manager.state: "Новые контакты менеджера (оплата Премиум):",
    AdminStates.edit_support.state: "Новый контакт поддержки (для заблокированных):",
}

_ADD_CHANNEL_HINT = (
    "📢 Добавление канала\n\n"
    "⚠️ Сначала сделай бота администратором канала — иначе нельзя проверить подписку.\n\n"
    "Затем пришли:\n"
    "• @ник канала\n"
    "• публичную ссылку t.me/ник\n"
    "• или перешли любое сообщение из канала"
)

_EDIT_STATE_FILTER = StateFilter(
    AdminStates.edit_limit,
    AdminStates.edit_dist,
    AdminStates.edit_reshow,
    AdminStates.edit_card,
    AdminStates.edit_check_time,
    AdminStates.edit_manager,
    AdminStates.edit_support,
)


def _is_admin(user_id: int) -> bool:
    return user_id in settings.admin_id_set


async def _redraw(
    callback: CallbackQuery, bot: Bot, text: str, kb: InlineKeyboardMarkup
) -> None:
    """Repaint the panel in place; a stale button gets a fresh screen instead."""
    message = callback.message
    if isinstance(message, Message):
        try:
            await message.edit_text(text, reply_markup=kb)
            return
        except TelegramBadRequest as exc:
            if "not modified" in str(exc).lower():
                return
    await bot.send_message(callback.from_user.id, text, reply_markup=kb)


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
            [InlineKeyboardButton(text="📢 Каналы", callback_data="adm:channels")],
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
            [InlineKeyboardButton(text="Контакт поддержки", callback_data="adm:edit:support")],
            [InlineKeyboardButton(text="Приветственный пост", callback_data="adm:edit:welcome")],
            [InlineKeyboardButton(text="⬅️ Назад", callback_data="adm:root")],
        ]
    )


def _cancel_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="❌ Отмена", callback_data="adm:settings")],
        ]
    )


def _cancel_channels_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="❌ Отмена", callback_data="adm:channels")],
        ]
    )


async def _channels_view(session: AsyncSession) -> tuple[str, InlineKeyboardMarkup]:
    channels = await list_all_channels(session)
    if not channels:
        text = (
            "📢 Обязательные каналы\n\n"
            "Список пуст.\n"
            "Бот должен быть админом каждого канала, иначе проверка подписки не работает."
        )
    else:
        lines = [
            "📢 Обязательные каналы\n",
            "Бот должен быть админом каждого канала.\n",
        ]
        for ch in channels:
            mark = "🟢" if ch.is_active else "🔴"
            title = html.escape(ch.title or ch.channel_id)
            channel_id = html.escape(ch.channel_id)
            lines.append(f"{mark} {title}\n   {channel_id}")
        text = "\n".join(lines)
    rows: list[list[InlineKeyboardButton]] = []
    for ch in channels:
        label = (ch.title or ch.channel_id)[:28]
        tog = "Выкл" if ch.is_active else "Вкл"
        rows.append(
            [
                _btn(f"{tog}: {label}", f"adm:ch:tog:{ch.id}"),
                _btn("🗑", f"adm:ch:del:{ch.id}"),
            ]
        )
    rows.append([InlineKeyboardButton(text="➕ Добавить", callback_data="adm:ch:add")])
    rows.append([InlineKeyboardButton(text="⬅️ Назад", callback_data="adm:root")])
    return text, InlineKeyboardMarkup(inline_keyboard=rows)


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


def _gender_stats_block(stats: dict[str, int]) -> str:
    """Left-aligned column; numbers padded for visual column."""
    m = stats["male"]
    f = stats["female"]
    z = stats["total"]
    width = max(len(str(m)), len(str(f)), len(str(z)), 1)
    return (
        f"<pre>"
        f"Парней:   {m:>{width}}\n"
        f"Девушек:  {f:>{width}}\n"
        f"Всего:    {z:>{width}}"
        f"</pre>"
    )


async def _root_view(session: AsyncSession) -> tuple[str, InlineKeyboardMarkup]:
    pending = await list_pending_orders(session)
    reg_only = await is_registration_only(session)
    stats = await count_profiles_by_gender(session)
    soft = "🟢 ON (только регистрация)" if reg_only else "🔴 OFF (лента открыта)"
    text = (
        "Админка Vinchik\n\n"
        f"{_gender_stats_block(stats)}\n"
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
    welcome = await get_welcome_post(session)
    soft = "🟢 ON" if reg_only else "🔴 OFF"
    if welcome_post_configured(welcome):
        w_preview = (welcome["text"] or "").strip() or "(без текста)"
        if len(w_preview) > 120:
            w_preview = w_preview[:117] + "…"
        welcome_line = f"Приветственный пост: ✅\n{html.escape(w_preview)}"
    else:
        welcome_line = "Приветственный пост: ❌ не задан (на /start — текст из локалей)"
    text = (
        "⚙️ Настройки\n\n"
        f"Soft-launch: {soft}\n"
        f"Лимит лайков / сутки UTC: {limit}\n"
        f"Радиус км: {dist:g}\n"
        f"Повтор анкеты (дней): {reshow}\n\n"
        f"{welcome_line}\n\n"
        f"Карта:\n{html.escape(pay['card'])}\n\n"
        f"Время проверки:\n{html.escape(pay['check_time'])}\n\n"
        f"Менеджер:\n{html.escape(pay['manager'])}\n\n"
        f"Поддержка:\n{html.escape(pay['support'])}"
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
        f"чек: {'есть' if order.receipt_file_id else 'нет'}\n"
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
    if message.from_user is None:
        return
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
async def adm_root(
    callback: CallbackQuery, session: AsyncSession, state: FSMContext, bot: Bot
) -> None:
    if not _is_admin(callback.from_user.id):
        await callback.answer(t("no_access", "ru"), show_alert=True)
        return
    await state.clear()
    await callback.answer()
    text, kb = await _root_view(session)
    await _redraw(callback, bot, text, kb)


@router.callback_query(F.data == "adm:settings")
async def adm_settings(
    callback: CallbackQuery, session: AsyncSession, state: FSMContext, bot: Bot
) -> None:
    if not _is_admin(callback.from_user.id):
        await callback.answer(t("no_access", "ru"), show_alert=True)
        return
    await state.clear()
    await callback.answer()
    text, kb = await _settings_view(session)
    await _redraw(callback, bot, text, kb)


@router.callback_query(F.data.startswith("adm:edit:"))
async def adm_edit_start(callback: CallbackQuery, state: FSMContext, bot: Bot) -> None:
    if not _is_admin(callback.from_user.id):
        await callback.answer(t("no_access", "ru"), show_alert=True)
        return
    field = (callback.data or "").split(":")[2]
    if field == "welcome":
        await state.set_state(AdminStates.edit_welcome)
        await callback.answer()
        await bot.send_message(
            callback.from_user.id,
            "👋 Приветственный пост (/start)\n\n"
            "Пришли фото с подписью — это и будет пост вместо текстового приветствия.\n"
            "Можно прислать только текст — обновится подпись (фото останется).\n"
            "Или только фото — подпись сохранится прежней.",
            reply_markup=_cancel_kb(),
        )
        return
    mapping = {
        "limit": AdminStates.edit_limit,
        "dist": AdminStates.edit_dist,
        "reshow": AdminStates.edit_reshow,
        "card": AdminStates.edit_card,
        "check_time": AdminStates.edit_check_time,
        "manager": AdminStates.edit_manager,
        "support": AdminStates.edit_support,
    }
    st = mapping.get(field)
    if st is None:
        await callback.answer("—", show_alert=True)
        return
    await state.set_state(st)
    await callback.answer()
    await bot.send_message(
        callback.from_user.id, _EDIT_PROMPTS[st.state], reply_markup=_cancel_kb()
    )


@router.message(StateFilter(AdminStates.edit_welcome), F.photo)
async def adm_edit_welcome_photo(
    message: Message, session: AsyncSession, state: FSMContext
) -> None:
    if message.from_user is None or not _is_admin(message.from_user.id):
        await state.clear()
        return
    if not message.photo:
        return
    file_id = message.photo[-1].file_id
    caption = (message.caption or "").strip()
    kwargs: dict = {"photo_file_id": file_id}
    if caption:
        kwargs["text"] = caption
    await set_welcome_post(session, **kwargs)
    await state.clear()
    text, kb = await _settings_view(session)
    await message.answer("✅ Приветственный пост сохранён.\n\n" + text, reply_markup=kb)


@router.message(StateFilter(AdminStates.edit_welcome), F.text)
async def adm_edit_welcome_text(
    message: Message, session: AsyncSession, state: FSMContext
) -> None:
    if message.from_user is None or not _is_admin(message.from_user.id):
        await state.clear()
        return
    raw = (message.text or "").strip()
    if not raw:
        await message.answer("Нужен текст подписи или фото с подписью.")
        return
    current = await get_welcome_post(session)
    if not welcome_post_configured(current):
        await message.answer(
            "Сначала пришли фото (с подписью). Один текст без картинки пост не включает."
        )
        return
    await set_welcome_post(session, text=raw)
    await state.clear()
    text, kb = await _settings_view(session)
    await message.answer("✅ Текст поста обновлён.\n\n" + text, reply_markup=kb)


@router.message(StateFilter(AdminStates.edit_welcome))
async def adm_edit_welcome_other(message: Message) -> None:
    if message.from_user is None or not _is_admin(message.from_user.id):
        return
    await message.answer("Нужно фото (желательно с подписью) или текст подписи.")


@router.message(_EDIT_STATE_FILTER, F.text)
async def adm_edit_value(
    message: Message, session: AsyncSession, state: FSMContext
) -> None:
    if message.from_user is None:
        return
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
        elif current == AdminStates.edit_support.state:
            if not raw:
                raise ValueError("empty")
            await set_setting(session, "support_contact", raw)
        else:
            await state.clear()
            return
    except ValueError:
        await message.answer("Некорректное значение. Попробуй ещё раз или отмени.")
        return

    await state.clear()
    text, kb = await _settings_view(session)
    await message.answer("✅ Сохранено.\n\n" + text, reply_markup=kb)


@router.callback_query(F.data == "adm:channels")
async def adm_channels(
    callback: CallbackQuery, session: AsyncSession, state: FSMContext, bot: Bot
) -> None:
    if not _is_admin(callback.from_user.id):
        await callback.answer(t("no_access", "ru"), show_alert=True)
        return
    await state.clear()
    await callback.answer()
    text, kb = await _channels_view(session)
    await _redraw(callback, bot, text, kb)


@router.callback_query(F.data == "adm:ch:add")
async def adm_ch_add_start(callback: CallbackQuery, state: FSMContext, bot: Bot) -> None:
    if not _is_admin(callback.from_user.id):
        await callback.answer(t("no_access", "ru"), show_alert=True)
        return
    await state.set_state(AdminStates.add_channel)
    await callback.answer()
    await bot.send_message(
        callback.from_user.id, _ADD_CHANNEL_HINT, reply_markup=_cancel_channels_kb()
    )


async def _finish_add_channel(
    message: Message,
    session: AsyncSession,
    state: FSMContext,
    bot: Bot,
    *,
    resolved,
) -> None:
    ch, created = await add_resolved_channel(session, resolved)
    await state.clear()
    verb = "добавлен" if created else "обновлён (уже был в списке)"
    text, kb = await _channels_view(session)
    await message.answer(
        f"✅ Канал {verb}: {html.escape(ch.title or ch.channel_id)}\n\n{text}",
        reply_markup=kb,
    )


@router.message(StateFilter(AdminStates.add_channel), F.forward_from_chat | F.forward_origin)
async def adm_ch_add_forward(
    message: Message, session: AsyncSession, state: FSMContext, bot: Bot
) -> None:
    if message.from_user is None:
        return
    if not _is_admin(message.from_user.id):
        await state.clear()
        return
    try:
        resolved = await resolve_channel_forward(bot, message)
    except ChannelResolveError as exc:
        await message.answer(str(exc))
        return
    await _finish_add_channel(message, session, state, bot, resolved=resolved)


@router.message(StateFilter(AdminStates.add_channel), F.text)
async def adm_ch_add_text(
    message: Message, session: AsyncSession, state: FSMContext, bot: Bot
) -> None:
    if message.from_user is None:
        return
    if not _is_admin(message.from_user.id):
        await state.clear()
        return
    try:
        resolved = await resolve_channel_ref(bot, message.text or "")
    except ChannelResolveError as exc:
        await message.answer(str(exc))
        return
    await _finish_add_channel(message, session, state, bot, resolved=resolved)


@router.message(StateFilter(AdminStates.add_channel))
async def adm_ch_add_other(message: Message) -> None:
    if not message.from_user or not _is_admin(message.from_user.id):
        return
    await message.answer(
        "Пришли @ник, ссылку t.me/… или перешли сообщение из канала.\n"
        "Не забудь: бот должен быть админом канала."
    )


@router.callback_query(F.data.startswith("adm:ch:tog:"))
async def adm_ch_toggle(callback: CallbackQuery, session: AsyncSession, bot: Bot) -> None:
    if not _is_admin(callback.from_user.id):
        await callback.answer(t("no_access", "ru"), show_alert=True)
        return
    pk = int((callback.data or "").split(":")[3])
    ch = await toggle_channel(session, pk)
    if ch is None:
        await callback.answer("Не найден", show_alert=True)
        return
    await callback.answer("ON" if ch.is_active else "OFF")
    text, kb = await _channels_view(session)
    await _redraw(callback, bot, text, kb)


@router.callback_query(F.data.startswith("adm:ch:del:"))
async def adm_ch_delete(callback: CallbackQuery, session: AsyncSession, bot: Bot) -> None:
    if not _is_admin(callback.from_user.id):
        await callback.answer(t("no_access", "ru"), show_alert=True)
        return
    pk = int((callback.data or "").split(":")[3])
    ok = await delete_channel(session, pk)
    await callback.answer("Удалён" if ok else "Не найден", show_alert=not ok)
    text, kb = await _channels_view(session)
    await _redraw(callback, bot, text, kb)


@router.callback_query(F.data.startswith("adm:orders:"))
async def adm_orders(callback: CallbackQuery, session: AsyncSession, bot: Bot) -> None:
    if not _is_admin(callback.from_user.id):
        await callback.answer(t("no_access", "ru"), show_alert=True)
        return
    index = int((callback.data or "").split(":")[2])
    text, kb = await _render_order(session, index)
    await callback.answer()
    await _redraw(callback, bot, text, kb)


async def _seal_receipt_notice(callback: CallbackQuery, line: str) -> None:
    """Mark the receipt push as done — do not open the /admin orders queue."""
    message = callback.message
    if not isinstance(message, Message):
        return
    try:
        await message.edit_reply_markup(reply_markup=None)
    except TelegramBadRequest:
        pass
    if message.caption is not None:
        caption = f"{message.caption}\n\n{line}"
        try:
            await message.edit_caption(caption=caption)
        except TelegramBadRequest:
            pass
        return
    if message.text:
        try:
            await message.edit_text(f"{message.text}\n\n{line}")
        except TelegramBadRequest:
            pass


@router.callback_query(F.data.startswith("adm:rok:"))
async def adm_receipt_ok(
    callback: CallbackQuery, session: AsyncSession, bot: Bot
) -> None:
    """Approve from the receipt notification (not the admin orders browser)."""
    if not _is_admin(callback.from_user.id):
        await callback.answer(t("no_access", "ru"), show_alert=True)
        return
    order_id = int((callback.data or "").split(":")[2])
    result = await approve_order(session, order_id, callback.from_user.id)
    if result:
        order, user = result
        await notify_premium_activated(bot, user)
        await callback.answer("Одобрено")
        await _seal_receipt_notice(callback, "✅ Одобрено")
    else:
        await callback.answer("Уже обработана", show_alert=True)
        await _seal_receipt_notice(callback, "ℹ️ Уже обработана")


@router.callback_query(F.data.startswith("adm:rno:"))
async def adm_receipt_no(callback: CallbackQuery, session: AsyncSession) -> None:
    """Reject from the receipt notification (not the admin orders browser)."""
    if not _is_admin(callback.from_user.id):
        await callback.answer(t("no_access", "ru"), show_alert=True)
        return
    order_id = int((callback.data or "").split(":")[2])
    order = await reject_order(session, order_id, callback.from_user.id)
    if order:
        await callback.answer("Отклонено")
        await _seal_receipt_notice(callback, "❌ Отклонено")
    else:
        await callback.answer("Уже обработана", show_alert=True)
        await _seal_receipt_notice(callback, "ℹ️ Уже обработана")


@router.callback_query(F.data.startswith("adm:ok:"))
async def adm_ok(callback: CallbackQuery, session: AsyncSession, bot: Bot) -> None:
    if not _is_admin(callback.from_user.id):
        await callback.answer(t("no_access", "ru"), show_alert=True)
        return
    parts = (callback.data or "").split(":")
    order_id = int(parts[2])
    index = int(parts[3]) if len(parts) > 3 else 0
    result = await approve_order(session, order_id, callback.from_user.id)
    if result:
        order, user = result
        await notify_premium_activated(bot, user)
        await callback.answer("Одобрено")
    else:
        await callback.answer("Уже обработана", show_alert=True)
    text, kb = await _render_order(session, index)
    await _redraw(callback, bot, text, kb)


@router.callback_query(F.data.startswith("adm:no:"))
async def adm_no(callback: CallbackQuery, session: AsyncSession, bot: Bot) -> None:
    if not _is_admin(callback.from_user.id):
        await callback.answer(t("no_access", "ru"), show_alert=True)
        return
    parts = (callback.data or "").split(":")
    order_id = int(parts[2])
    index = int(parts[3]) if len(parts) > 3 else 0
    order = await reject_order(session, order_id, callback.from_user.id)
    if order:
        await callback.answer("Отклонено")
    else:
        await callback.answer("Уже обработана", show_alert=True)
    text, kb = await _render_order(session, index)
    await _redraw(callback, bot, text, kb)


@router.callback_query(F.data.startswith("adm:premiums:"))
async def adm_premiums(callback: CallbackQuery, session: AsyncSession, bot: Bot) -> None:
    if not _is_admin(callback.from_user.id):
        await callback.answer(t("no_access", "ru"), show_alert=True)
        return
    page = int((callback.data or "").split(":")[2])
    text, kb = await _render_premiums(session, page)
    await callback.answer()
    await _redraw(callback, bot, text, kb)


@router.callback_query(F.data == "adm:toggle_reg")
async def adm_toggle_reg(callback: CallbackQuery, session: AsyncSession, bot: Bot) -> None:
    if not _is_admin(callback.from_user.id):
        await callback.answer(t("no_access", "ru"), show_alert=True)
        return
    current = await is_registration_only(session)
    await set_setting(session, "registration_only", "false" if current else "true")
    now_on = not current
    await callback.answer("Soft-launch ON" if now_on else "Soft-launch OFF")
    shown = callback.message.text if isinstance(callback.message, Message) else None
    if (shown or "").startswith("⚙️"):
        text, kb = await _settings_view(session)
    else:
        text, kb = await _root_view(session)
    await _redraw(callback, bot, text, kb)
