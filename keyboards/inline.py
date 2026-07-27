from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup, KeyboardButton, ReplyKeyboardMarkup

from config import settings
from locales import t


def language_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text=t("lang_tg", "tg"), callback_data="lang:tg"),
                InlineKeyboardButton(text=t("lang_ru", "ru"), callback_data="lang:ru"),
            ]
        ]
    )


def keep_kb(lang: str, callback: str = "keep") -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[[InlineKeyboardButton(text=t("keep_current", lang), callback_data=callback)]]
    )


def gender_kb(lang: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text=t("gender_male", lang), callback_data="gender:male"),
                InlineKeyboardButton(text=t("gender_female", lang), callback_data="gender:female"),
            ]
        ]
    )


def looking_kb(lang: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text=t("looking_female", lang), callback_data="looking:female")],
            [InlineKeyboardButton(text=t("looking_male", lang), callback_data="looking:male")],
            [InlineKeyboardButton(text=t("looking_any", lang), callback_data="looking:any")],
        ]
    )


def location_kb(lang: str, has_current: bool) -> tuple[ReplyKeyboardMarkup, InlineKeyboardMarkup | None]:
    reply = ReplyKeyboardMarkup(
        keyboard=[[KeyboardButton(text=t("send_location", lang), request_location=True)]],
        resize_keyboard=True,
        one_time_keyboard=True,
    )
    inline = keep_kb(lang, "keep:location") if has_current else None
    return reply, inline


def about_kb(lang: str, has_current: bool) -> InlineKeyboardMarkup:
    rows = [[InlineKeyboardButton(text=t("skip_text", lang), callback_data="about:skip")]]
    if has_current:
        rows.append([InlineKeyboardButton(text=t("keep_current", lang), callback_data="keep:about")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def photo_keep_kb(lang: str) -> InlineKeyboardMarkup:
    return keep_kb(lang, "keep:photo")


def my_profile_kb(lang: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text=t("btn_refill", lang), callback_data="profile:refill")],
            [InlineKeyboardButton(text=t("btn_edit_photo", lang), callback_data="profile:edit_photo")],
            [InlineKeyboardButton(text=t("btn_edit_text", lang), callback_data="profile:edit_text")],
            [InlineKeyboardButton(text=t("btn_browse", lang), callback_data="browse:start")],
            [InlineKeyboardButton(text=t("btn_main_menu", lang), callback_data="menu:root")],
        ]
    )


def browse_kb(lang: str, target_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text=t("btn_like", lang), callback_data=f"b:like:{target_id}"),
                InlineKeyboardButton(text=t("btn_message", lang), callback_data=f"b:msg:{target_id}"),
                InlineKeyboardButton(text=t("btn_dislike", lang), callback_data=f"b:no:{target_id}"),
                InlineKeyboardButton(text=t("btn_sleep", lang), callback_data="b:sleep"),
            ],
            [
                InlineKeyboardButton(
                    text=t("btn_report", lang),
                    callback_data=f"b:rep:{target_id}",
                )
            ],
        ]
    )


def empty_feed_kb(lang: str) -> InlineKeyboardMarkup:
    rows = [[InlineKeyboardButton(text=t("menu_browse", lang), callback_data="browse:start")]]
    if settings.bot_username:
        rows.append(
            [
                InlineKeyboardButton(
                    text=t("menu_share", lang),
                    url=f"https://t.me/{settings.bot_username}",
                )
            ]
        )
    rows.append([InlineKeyboardButton(text=t("back", lang), callback_data="menu:root")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def main_menu_kb(lang: str) -> InlineKeyboardMarkup:
    rows = [
        [InlineKeyboardButton(text=t("menu_browse", lang), callback_data="browse:start")],
        [InlineKeyboardButton(text=t("menu_my", lang), callback_data="menu:my")],
        [InlineKeyboardButton(text=t("menu_premium", lang), callback_data="menu:premium")],
        [InlineKeyboardButton(text=t("menu_settings", lang), callback_data="menu:settings")],
        [InlineKeyboardButton(text=t("menu_stop", lang), callback_data="menu:stop")],
    ]
    if settings.bot_username:
        rows.append(
            [
                InlineKeyboardButton(
                    text=t("menu_share", lang),
                    url=f"https://t.me/{settings.bot_username}",
                )
            ]
        )
    return InlineKeyboardMarkup(inline_keyboard=rows)


def settings_kb(lang: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text=t("settings_language", lang), callback_data="settings:lang")],
            [InlineKeyboardButton(text=t("back", lang), callback_data="menu:root")],
        ]
    )


def stop_confirm_kb(lang: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text=t("stop_yes", lang), callback_data="stop:yes")],
            [InlineKeyboardButton(text=t("stop_no", lang), callback_data="menu:root")],
        ]
    )


def channels_kb(lang: str, channels) -> InlineKeyboardMarkup:
    rows = []
    for ch in channels:
        link = ch.invite_link or (
            f"https://t.me/{ch.channel_id.lstrip('@')}" if not str(ch.channel_id).startswith("-") else None
        )
        title = ch.title or ch.channel_id
        if link:
            rows.append([InlineKeyboardButton(text=f"{t('btn_subscribe', lang)}: {title}", url=link)])
    rows.append([InlineKeyboardButton(text=t("btn_subscribed", lang), callback_data="channels:check")])
    return InlineKeyboardMarkup(inline_keyboard=rows)
